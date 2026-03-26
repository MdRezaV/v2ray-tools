#!/usr/bin/env python3
"""
v2cidr.py - Organize V2Ray configs by country using MMDB or CIDR ranges

This script processes V2Ray configuration URIs from text files, resolves server
domains to IP addresses, determines the country of each IP using either a MaxMind
GeoLite2 database or CIDR range files, and writes the configs into country-specific
output files. It supports multi-threading for speed and can deduplicate identical
configs.

Usage:
  python v2cidr.py configs/*.txt
  python v2cidr.py *.txt -w 8 -s
  python v2cidr.py server1.txt server2.txt -v

Installation of required packages:
  pip install python-v2ray pycountry rich maxminddb

For more details, see the argparse help (-h) or the module docstring below.
"""

import argparse
import socket
import ipaddress
import sys
import shutil
import logging
import glob
import threading
import time
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections import defaultdict, deque
from typing import Dict, List, Set, Tuple, Optional, Any

# Third-party imports with enhanced error handling
try:
    from python_v2ray.config_parser import load_configs, deduplicate_configs
    import pycountry
    from rich.console import Console, Group
    from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
    from rich.live import Live
    from rich.table import Table
    from rich import box
    import maxminddb
except ImportError as e:
    missing = str(e).split("'")[1] if "'" in str(e) else "unknown"
    print(f"\n❌ Error: Missing required package: {missing}\n")
    print("Please install all dependencies with:\n")
    print("  pip install python-v2ray pycountry rich maxminddb\n")
    print("If you already have them installed, ensure they are in your current Python environment.\n")
    sys.exit(1)

# ===== MODIFIED: Force standard console colors =====
# Initialize console with color_system="standard" to use only the basic ANSI colors
# that match your terminal emulator theme.
console = Console(color_system="standard")
# ===================================================

# Suppress third-party warnings and verbose logging
import warnings
warnings.filterwarnings("ignore", category=UserWarning)
logging.getLogger("python_v2ray").setLevel(logging.ERROR)
logging.getLogger().setLevel(logging.ERROR)

# Global shutdown flag for graceful interruption
shutdown_flag = threading.Event()

# Global reference to the currently active Live instance (if any)
_current_live = None

def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully: allow current tasks to finish before exit."""
    global _current_live
    if not shutdown_flag.is_set():
        # Use the live console if available, otherwise fallback to global console
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

class ThreadSafeDict:
    """Thread-safe dictionary for counting occurrences by key."""
    def __init__(self):
        self._dict = defaultdict(int)
        self._lock = threading.Lock()

    def increment(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._dict[key] += amount

    def items(self) -> List[Tuple[str, int]]:
        with self._lock:
            return list(self._dict.items())

    def __len__(self) -> int:
        with self._lock:
            return len(self._dict)

# ============================================================================
# Core geolocation and file handling functions
# ============================================================================

def load_mmdb_database(mmdb_path: Path) -> Optional[Any]:
    """
    Load MaxMind GeoLite2 Country database.

    Args:
        mmdb_path: Path to the .mmdb file.

    Returns:
        maxminddb.reader object or None if loading fails.
    """
    if not mmdb_path.exists():
        console.print(f"[red]❌ MMDB file not found: {mmdb_path}[/red]")
        console.print("[yellow]💡 Hint: Download GeoLite2-Country.mmdb from https://dev.maxmind.com/geoip/geolite2-free-geolocation-data[/yellow]")
        return None
    try:
        console.print(f"[blue]Loading MMDB database from {mmdb_path}...[/blue]")
        reader = maxminddb.open_database(str(mmdb_path))
        console.print(f"[green]✨ MMDB database loaded successfully[/green]")
        return reader
    except Exception as e:
        console.print(f"[red]❌ Failed to load MMDB database: {e}[/red]")
        console.print("[yellow]💡 Hint: Ensure the file is a valid MaxMind DB format (e.g., GeoLite2-Country.mmdb).[/yellow]")
        return None

def load_cidr_ranges(cidr_dir: Path) -> Dict[str, Dict[str, List]]:
    """
    Load CIDR ranges from text files in the specified directory.

    File naming convention: <2-letter-country-code>[.ipv6].txt
    e.g., US.txt for IPv4, US.ipv6.txt for IPv6.

    Args:
        cidr_dir: Directory containing CIDR files.

    Returns:
        Dictionary: {country_code: {"ipv4": [networks], "ipv6": [networks]}}
    """
    country_cidrs = defaultdict(lambda: {"ipv4": [], "ipv6": []})
    if not cidr_dir.exists():
        console.print(f"[red]❌ CIDR directory not found: {cidr_dir}[/red]")
        console.print("[yellow]💡 Hint: Create this directory and place CIDR range files (e.g., US.txt, US.ipv6.txt) inside.[/yellow]")
        return {}

    console.print(f"[blue]Loading CIDR ranges from {cidr_dir}...[/blue]")
    file_count = 0
    cidr_count = 0

    for cidr_file in cidr_dir.glob("*.txt"):
        if shutdown_flag.is_set():
            break
        filename = cidr_file.stem
        if len(filename) < 2:
            continue
        country_code = filename[:2].upper()
        is_ipv6 = "ipv6" in filename.lower()
        try:
            with open(cidr_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if shutdown_flag.is_set():
                        break
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    try:
                        network = (ipaddress.IPv6Network(line, strict=False) if is_ipv6
                                   else ipaddress.IPv4Network(line, strict=False))
                        country_cidrs[country_code][("ipv6" if is_ipv6 else "ipv4")].append(network)
                        cidr_count += 1
                    except ValueError:
                        # Silently skip malformed lines
                        pass
            file_count += 1
        except IOError:
            pass

    if shutdown_flag.is_set():
        return {}
    if country_cidrs:
        console.print(f"[green]✨ Loaded {cidr_count:,} CIDR ranges from {file_count} files for {len(country_cidrs)} countries[/green]")
    else:
        console.print("[red]❌ No CIDR ranges loaded[/red]")
    return dict(country_cidrs)

def resolve_domain_to_ip(domain: str, timeout: int = 5, retry_count: int = 5,
                         dns_lookup_counter: Optional[ThreadSafeCounter] = None) -> Optional[ipaddress._BaseAddress]:
    """
    Resolve a domain name to an IP address with retries and timeout.

    If the input is already an IP, it is returned immediately without counting.
    DNS lookups are counted exactly once per domain (not per retry).

    Args:
        domain: Domain name or IP string.
        timeout: Socket timeout in seconds.
        retry_count: Number of retries after initial attempt.
        dns_lookup_counter: Optional counter to track total DNS lookups.

    Returns:
        ipaddress.IPv4Address or IPv6Address, or None if resolution fails.
    """
    # Check if it's already an IP – no DNS lookup needed
    try:
        return ipaddress.ip_address(domain)
    except ValueError:
        pass

    # We have a domain name – count this DNS lookup (once)
    if dns_lookup_counter:
        dns_lookup_counter.increment()

    max_attempts = retry_count + 1
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(2)  # wait before retry
        try:
            socket.setdefaulttimeout(timeout)
            # Try IPv4 first (fast path)
            ipv4 = socket.gethostbyname(domain)
            return ipaddress.IPv4Address(ipv4)
        except socket.gaierror:
            # Fallback to getaddrinfo for both families
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
    return None

def get_country_for_ip_mmdb(ip: ipaddress._BaseAddress, mmdb_reader: Any) -> str:
    """
    Determine country code using MaxMind MMDB.

    Args:
        ip: IP address object.
        mmdb_reader: Opened maxminddb reader.

    Returns:
        ISO 3166-1 alpha-2 country code or "UNKNOWN".
    """
    try:
        result = mmdb_reader.get(str(ip))
        if result:
            # Try primary country field
            if 'country' in result and result['country']:
                code = result['country'].get('iso_code')
                if code:
                    return code
            # Fallback to registered country
            if 'registered_country' in result and result['registered_country']:
                code = result['registered_country'].get('iso_code')
                if code:
                    return code
    except Exception:
        pass
    return "UNKNOWN"

def get_country_for_ip(ip: ipaddress._BaseAddress, cidr_ranges: Dict) -> str:
    """
    Determine country code using CIDR range files.

    Args:
        ip: IP address object.
        cidr_ranges: Loaded CIDR data.

    Returns:
        Country code or "UNKNOWN".
    """
    ip_type = "ipv4" if isinstance(ip, ipaddress.IPv4Address) else "ipv6"
    for country_code, networks in cidr_ranges.items():
        for network in networks.get(ip_type, []):
            if ip in network:
                return country_code
    return "UNKNOWN"

def get_country_name(country_code: str) -> str:
    """
    Convert ISO country code to full country name with underscores for spaces.

    Args:
        country_code: 2-letter code (e.g., "US").

    Returns:
        Country name with spaces replaced by underscores, or the code if unknown.
    """
    if country_code == "UNKNOWN":
        return "Unknown"
    try:
        country = pycountry.countries.get(alpha_2=country_code)
        if country:
            return country.name.replace(" ", "_")
    except (KeyError, AttributeError):
        pass
    return country_code

def setup_output_directory(output_dir: Path) -> None:
    """
    Prepare output directory: backup previous if exists, create fresh.

    Args:
        output_dir: Directory for output files.
    """
    old_dir = output_dir.with_name(f"{output_dir.name}-old")
    if old_dir.exists():
        shutil.rmtree(old_dir)
    if output_dir.exists():
        output_dir.rename(old_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]✨ Output directory ready: {output_dir}[/green]")

def expand_file_patterns(patterns: List[str]) -> List[Path]:
    """
    Expand glob patterns to actual file paths.

    Args:
        patterns: List of file patterns (e.g., "*.txt").

    Returns:
        List of Path objects for existing files.
    """
    files = []
    for pattern in patterns:
        if shutdown_flag.is_set():
            break
        matched = glob.glob(pattern)
        if not matched:
            # Assume literal file name
            files.append(Path(pattern))
        else:
            for p in matched:
                path = Path(p)
                if path.is_file():
                    files.append(path)
    # Remove duplicates while preserving order
    return list(dict.fromkeys(files))

def create_stats_table(stats: Dict, elapsed_time: float = 0,
                       processing_speed: float = 0, total_configs: int = 0,
                       completed_count: int = 0) -> Table:
    """
    Build a Rich Table with real-time statistics.

    Args:
        stats: Current statistics dictionary.
        elapsed_time: Time elapsed in seconds.
        processing_speed: Current processing speed (configs/second).
        total_configs: Total number of tasks.
        completed_count: Number of tasks completed.

    Returns:
        Rich Table object.
    """
    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 1), border_style="cyan")
    table.add_column("Metric", style="blue", width=25)
    table.add_column("Value", style="white")

    if total_configs > 0:
        percent = (completed_count / total_configs) * 100
        table.add_row("Progress", f"[cyan]{percent:.1f}% ({completed_count:,}/{total_configs:,})[/cyan]")

    table.add_row("Valid Configs", f"[white]{stats.get('processed_configs', 0):,}[/white]")

    if stats.get('dns_lookups', 0) > 0:
        table.add_row("DNS Lookups", f"[cyan]{stats.get('dns_lookups', 0):,}[/cyan]")

    if stats.get('dns_errors', 0) > 0 and completed_count > 0:
        error_pct = (stats['dns_errors'] / completed_count) * 100
        table.add_row("Error (DNS)", f"[red]{stats.get('dns_errors', 0):,} ({error_pct:.1f}%)[/red]")

    unknown_count = stats.get('by_country', {}).get('UNKNOWN', 0)
    if unknown_count > 0 and completed_count > 0:
        unknown_pct = (unknown_count / completed_count) * 100
        table.add_row("Unknown", f"[cyan]{unknown_count:,} ({unknown_pct:.1f}%)[/cyan]")

    if processing_speed > 0:
        table.add_row("Speed", f"[blue]{processing_speed:.1f} configs/s[/blue]")

    if elapsed_time > 0 and total_configs > 0 and completed_count > 0:
        time_per_config = elapsed_time / completed_count
        remaining = time_per_config * (total_configs - completed_count)
        elapsed_str = f"{elapsed_time:.1f}s" if elapsed_time < 60 else f"{elapsed_time/60:.1f}m" if elapsed_time < 3600 else f"{elapsed_time/3600:.1f}h"
        est_str = f"{remaining:.0f}s" if remaining < 60 else f"{remaining/60:.0f}m" if remaining < 3600 else f"{remaining/3600:.1f}h"
        table.add_row("Time", f"[white]{elapsed_str}[/white] [blue](est. {est_str})[/blue]")

    return table

def get_config_key(config):
    """
    Generate a unique key for a config based on its identity fields.

    Used for deduplication: server address, port, protocol, and credential.
    """
    base = (config.address, config.port, config.protocol)
    if hasattr(config, 'id'):
        return base + (config.id,)
    if hasattr(config, 'password'):
        return base + (config.password,)
    return base  # fallback (should not happen for valid configs)

def process_config_line(args: Tuple) -> Optional[str]:
    """
    Worker function: resolve domain, determine country, and write to output.

    Args:
        args: Tuple containing (config, original_line, lookup_data, lookup_type,
              file_locks, output_dir, shared_state)

    Returns:
        Country code or None if skipped.
    """
    config, original_line, lookup_data, lookup_type, file_locks, output_dir, shared_state = args
    if shutdown_flag.is_set():
        return None

    dns_errors = shared_state["dns_errors_counter"]
    dns_lookups = shared_state["dns_lookups_counter"]
    other_skips = shared_state["other_skips_counter"]
    country_counter = shared_state["country_counter"]
    processed_counter = shared_state["processed_counter"]
    error_writer = shared_state["error_writer"]

    address = config.address
    port = config.port if hasattr(config, 'port') else None
    if not address or port is None:
        other_skips.increment()
        return None

    # Resolve domain to IP (with retries)
    ip = resolve_domain_to_ip(address, timeout=5, retry_count=1,
                              dns_lookup_counter=dns_lookups)
    if not ip:
        dns_errors.increment()
        error_writer.add_error(original_line)
        return None

    # Determine country
    if lookup_type == "mmdb":
        country_code = get_country_for_ip_mmdb(ip, lookup_data)
    else:
        country_code = get_country_for_ip(ip, lookup_data)

    country_counter.increment(country_code)
    processed_counter.increment()

    country_name = get_country_name(country_code)
    output_file = output_dir / f"{country_code}.{country_name}.txt"
    if output_file not in file_locks:
        file_locks[output_file] = threading.Lock()
    with file_locks[output_file]:
        with open(output_file, 'a', encoding='utf-8') as f:
            f.write(original_line + '\n')

    return country_code

class ErrorWriter:
    """Thread-safe writer for DNS error lines."""
    def __init__(self, error_file: Path):
        self.error_file = error_file
        self._lock = threading.Lock()

    def add_error(self, line: str) -> None:
        with self._lock:
            try:
                with open(self.error_file, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except IOError:
                pass

# ============================================================================
# Main processing function
# ============================================================================

def process_files(files: List[Path], lookup_data: Any, lookup_type: str,
                  output_dir: Path, skip_duplicates: bool = False,
                  workers: int = 4) -> Dict:
    """
    Process all input files, organize configs by country using multi-threading.

    Args:
        files: List of input file paths.
        lookup_data: MMDB reader or CIDR dict.
        lookup_type: "mmdb" or "cidr".
        output_dir: Output directory.
        skip_duplicates: If True, remove functionally identical configs.
        workers: Number of worker threads.

    Returns:
        Statistics dictionary.
    """
    global _current_live

    stats = {
        "total_files": 0,
        "processed_files": 0,
        "total_lines": 0,
        "processed_configs": ThreadSafeCounter(),
        "dns_lookups": ThreadSafeCounter(),
        "dns_errors": ThreadSafeCounter(),
        "other_skips": ThreadSafeCounter(),
        "duplicate_lines": 0,
        "invalid_lines": ThreadSafeCounter(),
        "by_country": ThreadSafeDict(),
    }

    # Read all lines from files
    if not files:
        console.print("[yellow]⚠️ No files to process[/yellow]")
        return {}

    all_lines = []
    console.print(f"\n[blue]Reading {len(files):,} file(s)...[/blue]")
    for file_path in files:
        if shutdown_flag.is_set():
            break
        stats["total_files"] += 1
        if not file_path.exists() or file_path.suffix.lower() != '.txt':
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
        return {}

    console.print(f"\n[green]✨ Total configs to process: {stats['total_lines']:,}[/green]")

    # Parse all lines into config objects
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

    # Deduplicate if requested
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
        return {}

    total_tasks = len(valid_configs)

    # Setup error writer
    error_file = output_dir / "Error.txt"
    error_writer = ErrorWriter(error_file)

    # Shared state for threads
    shared_state = {
        "dns_errors_counter": stats["dns_errors"],
        "dns_lookups_counter": stats["dns_lookups"],
        "other_skips_counter": stats["other_skips"],
        "country_counter": stats["by_country"],
        "processed_counter": stats["processed_configs"],
        "error_writer": error_writer,
    }

    file_locks: Dict[Path, threading.Lock] = {}

    console.print(f"\n[magenta]Starting processing with {workers} workers...[/magenta]")
    console.print("[blue]Press Ctrl+C to interrupt\n[/blue]")

    start_time = time.time()
    speed_history = deque()

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="Worker") as executor:
            task_args = [(config, line, lookup_data, lookup_type, file_locks, output_dir,
                          shared_state) for config, line in valid_configs]
            futures = [executor.submit(process_config_line, args) for args in task_args]

            completed_count = 0
            last_update_time = start_time

            # Progress bar and live display
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
                _current_live = live  # Store reference for signal handler
                try:
                    # Initial empty display
                    current_stats = {
                        "processed_configs": 0,
                        "dns_lookups": 0,
                        "dns_errors": 0,
                        "other_skips": 0,
                        "invalid_lines": stats["invalid_lines"].value,
                        "by_country": {},
                    }
                    live.update(Group(create_stats_table(current_stats, 0, 0, total_tasks, 0), progress))

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
                                "processed_configs": stats["processed_configs"].value,
                                "dns_lookups": stats["dns_lookups"].value,
                                "dns_errors": stats["dns_errors"].value,
                                "other_skips": stats["other_skips"].value,
                                "invalid_lines": stats["invalid_lines"].value,
                                "by_country": dict(stats["by_country"].items()),
                            }
                            if len(speed_history) >= 2:
                                oldest_time, oldest_count = speed_history[0]
                                window = now - oldest_time
                                speed = (completed_count - oldest_count) / window if window > 0 else 0
                            else:
                                speed = completed_count / elapsed if elapsed > 0 else 0
                            live.update(Group(create_stats_table(current_stats, elapsed, speed, total_tasks, completed_count), progress))
                            last_update_time = now

                        # Propagate exceptions
                        try:
                            future.result(timeout=1)
                        except Exception:
                            pass

                    # Final update
                    if not shutdown_flag.is_set():
                        elapsed = time.time() - start_time
                        current_stats = {
                            "processed_configs": stats["processed_configs"].value,
                            "dns_lookups": stats["dns_lookups"].value,
                            "dns_errors": stats["dns_errors"].value,
                            "other_skips": stats["other_skips"].value,
                            "invalid_lines": stats["invalid_lines"].value,
                            "by_country": dict(stats["by_country"].items()),
                        }
                        avg_speed = completed_count / elapsed if elapsed > 0 else 0
                        progress.update(progress_task, completed=total_tasks)
                        live.update(Group(create_stats_table(current_stats, elapsed, avg_speed, total_tasks, completed_count), progress))
                        time.sleep(0.5)
                finally:
                    _current_live = None  # Clear reference when done

    except KeyboardInterrupt:
        shutdown_flag.set()
        # The signal handler already printed a message; we can just continue
        console.print("\n[yellow]⚠️ Processing interrupted[/yellow]")

    elapsed_time = time.time() - start_time
    if elapsed_time > 0 and total_tasks > 0:
        avg_speed = total_tasks / elapsed_time
        console.print(f"\n[blue]Average speed: {avg_speed:.1f} configs/second[/blue]")
        console.print(f"[white]Total time: {elapsed_time:.1f} seconds[/white]")

    return {
        "total_files": stats["total_files"],
        "processed_files": stats["processed_files"],
        "total_lines": stats["total_lines"],
        "processed_configs": stats["processed_configs"].value,
        "dns_lookups": stats["dns_lookups"].value,
        "dns_errors": stats["dns_errors"].value,
        "other_skips": stats["other_skips"].value,
        "duplicate_lines": stats["duplicate_lines"],
        "invalid_lines": stats["invalid_lines"].value,
        "by_country": dict(stats["by_country"].items()),
    }

def print_final_statistics(stats: Dict, skip_duplicates: bool = False) -> None:
    """Print final summary and top countries."""
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]✨ FINAL RESULTS[/bold magenta]")
    console.print("=" * 60)

    total_processed = (stats['processed_configs'] + stats['dns_errors'] +
                       stats['other_skips'] + stats['duplicate_lines'] +
                       stats['invalid_lines'])
    # Avoid division by zero
    if stats["total_lines"] > 0:
        success_rate = (stats["processed_configs"] / stats["total_lines"]) * 100
    else:
        success_rate = 0.0

    summary = Table(box=box.ROUNDED, show_header=False, border_style="cyan")
    summary.add_column("Metric", style="blue", width=25)
    summary.add_column("Value", style="white")
    summary.add_row("Success Rate", f"[green]{success_rate:.1f}%[/green]")
    summary.add_row("Valid Configs", f"[white]{stats['processed_configs']:,}[/white]")
    summary.add_row("Total Configs", f"[blue]{stats['total_lines']:,}[/blue]")

    if stats["dns_lookups"] > 0:
        summary.add_row("DNS Lookups", f"[cyan]{stats['dns_lookups']:,}[/cyan]")
    if stats["dns_errors"] > 0 and stats["total_lines"] > 0:
        error_pct = (stats["dns_errors"] / stats["total_lines"]) * 100
        summary.add_row("Error (DNS)", f"[red]{stats['dns_errors']:,} ({error_pct:.1f}%)[/red]")
    if stats["other_skips"] > 0:
        summary.add_row("Other Skips", f"[cyan]{stats['other_skips']:,}[/cyan]")
    unknown_count = stats["by_country"].get('UNKNOWN', 0)
    if unknown_count > 0 and stats["total_lines"] > 0:
        unknown_pct = (unknown_count / stats["total_lines"]) * 100
        summary.add_row("Unknown", f"[cyan]{unknown_count:,} ({unknown_pct:.1f}%)[/cyan]")
    summary.add_row("Duplicate Lines", f"[yellow]{stats['duplicate_lines']:,}[/yellow]")
    if stats["by_country"]:
        summary.add_row("Countries Found", f"[blue]{len(stats['by_country'])}[/blue]")
    console.print(summary)

    # Top 10 countries
    if stats["by_country"]:
        total_for_percentage = max(stats["processed_configs"], 1)
        sorted_countries = sorted(stats["by_country"].items(), key=lambda x: x[1], reverse=True)[:10]
        console.print(f"\n[bold magenta]✨ Top 10 Countries:[/bold magenta]")
        for rank, (code, count) in enumerate(sorted_countries, 1):
            name = get_country_name(code).replace("_", " ")
            pct = (count / total_for_percentage) * 100
            icon = ["🌌", "🌠", "💫"][rank-1] if rank <= 3 else "✨"
            color = ["[magenta]", "[blue]", "[cyan]"][rank-1] if rank <= 3 else "[white]"
            console.print(f"  {color}{icon:3} {code:7} - [white]{name:35}[/white] [blue]{count:6,}[/blue] [cyan]({pct:5.1f}%)[/cyan]")

# ============================================================================
# Main entry point
# ============================================================================

def main():
    """Parse arguments and orchestrate processing."""
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser(
        description="Organize V2Ray configs by country using MMDB or CIDR ranges",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s configs/*.txt
  %(prog)s *.txt -w 8 -s
  %(prog)s server1.txt server2.txt -v
  %(prog)s *.txt --use-cidr --cidr-dir ./cidr
"""
    )

    # Input files
    parser.add_argument("files", nargs="+", type=str,
                        help="Text files with V2Ray URIs (one per line). Supports glob patterns like *.txt")

    # Geolocation method group
    geo_group = parser.add_argument_group("Geolocation Method")
    geo_excl = geo_group.add_mutually_exclusive_group()
    geo_excl.add_argument("--use-mmdb", action="store_true", default=True,
                          help="Use MMDB database for IP geolocation (default)")
    geo_excl.add_argument("--use-cidr", action="store_true",
                          help="Use CIDR ranges instead of MMDB")
    geo_group.add_argument("--mmdb-file", type=Path, default=Path("GeoLite2-Country.mmdb"),
                           help="Path to GeoLite2 MMDB file (default: GeoLite2-Country.mmdb)")
    geo_group.add_argument("--cidr-dir", type=Path, default=Path("cidr"),
                           help="Directory containing CIDR range files (default: cidr)")

    # Processing options
    proc_group = parser.add_argument_group("Processing Options")
    proc_group.add_argument("-w", "--workers", type=int, default=4,
                            help="Number of worker threads (default: 4, max: 32)")
    proc_group.add_argument("-s", "--skip-duplicates", action="store_true",
                            dest="skip_duplicates",
                            help="Skip configs with duplicate server:port combinations (using library deduplication)")
    proc_group.add_argument("-v", "--verbose", action="store_true",
                            help="Show detailed progress and warnings")

    # Output options
    out_group = parser.add_argument_group("Output Options")
    out_group.add_argument("--output-dir", type=Path, default=Path("by-country"),
                           help="Output directory (default: by-country)")

    args = parser.parse_args()

    # Validate workers
    if args.workers < 1 or args.workers > 32:
        console.print("[red]❌ Error: Workers must be between 1 and 32[/red]")
        sys.exit(1)

    use_mmdb = not args.use_cidr  # MMDB is default

    console.print("[bold magenta]🚀 V2Ray Config Organizer[/bold magenta]")
    console.print("[blue]" + "=" * 60 + "[/blue]")
    if args.skip_duplicates:
        console.print("[blue]✓ Skipping duplicate configs[/blue]")
    console.print(f"[green]✓ Using {args.workers} worker threads[/green]")
    if use_mmdb:
        console.print(f"[green]✓ Using MMDB database: {args.mmdb_file}[/green]")
    else:
        console.print(f"[green]✓ Using CIDR ranges from: {args.cidr_dir}[/green]")

    # Expand input files
    console.print("\n[blue]📁 Searching for files...[/blue]")
    expanded_files = expand_file_patterns(args.files)
    if not expanded_files:
        console.print("[red]❌ No files found to process[/red]")
        sys.exit(1)
    console.print(f"[green]✨ Found {len(expanded_files):,} file(s)[/green]")

    # Load geolocation data
    console.print("\n[bold magenta]🌍 Loading geolocation data...[/bold magenta]")
    if use_mmdb:
        lookup_data = load_mmdb_database(args.mmdb_file)
        lookup_type = "mmdb"
        if not lookup_data:
            sys.exit(1)
    else:
        lookup_data = load_cidr_ranges(args.cidr_dir)
        lookup_type = "cidr"
        if not lookup_data:
            sys.exit(1)

    # Setup output
    console.print("\n[blue]📂 Preparing output...[/blue]")
    setup_output_directory(args.output_dir)

    # Process files
    console.print("\n[bold magenta]⚡ Starting processing...[/bold magenta]")
    stats = process_files(
        files=expanded_files,
        lookup_data=lookup_data,
        lookup_type=lookup_type,
        output_dir=args.output_dir,
        skip_duplicates=args.skip_duplicates,
        workers=args.workers
    )

    if not stats or stats["total_lines"] == 0:
        console.print("[yellow]⚠️ Processing was interrupted or no configs found[/yellow]")
        sys.exit(1)

    print_final_statistics(stats, args.skip_duplicates)

    # Cleanup
    if use_mmdb and lookup_data:
        try:
            lookup_data.close()
        except Exception:
            pass

    # Final message
    console.print("\n" + "=" * 60)
    console.print(f"[green]✨ Success! Output saved to: {args.output_dir}[/green]")
    error_file = args.output_dir / "Error.txt"
    if error_file.exists():
        error_count = sum(1 for _ in open(error_file, 'r', encoding='utf-8'))
        if error_count > 0:
            console.print(f"[yellow]⚠️  {error_count:,} DNS errors written to: Error.txt[/yellow]")

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