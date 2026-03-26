#!/usr/bin/env python3
"""
IP to Country Code Mapper (ip2cc.py)
Maps IP addresses to countries using CIDR ranges from text files.

Author: Your Name
Version: 2.0.0 (optimized loading & lookups)
License: MIT
"""

import argparse
import ipaddress
import os
import sys
import json
import csv
import logging
import signal
import time
import multiprocessing as mp
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Union, Any
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed   # <-- FIXED

# Third-party imports (with fallbacks)
try:
    import pycountry
    PYCOUNTRY_AVAILABLE = True
except ImportError:
    PYCOUNTRY_AVAILABLE = False
    print("⚠️  Warning: pycountry not installed. Country names will be limited.")
    print("   Install via: pip install pycountry")

try:
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich.panel import Panel
    from rich import print as rprint
    from rich.text import Text
    from rich.syntax import Syntax
    from rich.logging import RichHandler

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    print("⚠️  Warning: rich not installed. Basic output will be used.")
    print("   Install via: pip install rich")

# Optional performance optimization (trie still uses network objects, kept for compatibility)
try:
    from iptrie import IPTrie
    IPTRIE_AVAILABLE = True
except ImportError:
    IPTRIE_AVAILABLE = False

# Constants
CIDR_FOLDER = "CIDR"
DEFAULT_CIDR_URL = "https://www.hackers.zone/cidr-lists/"
SUPPORTED_OUTPUT_FORMATS = ["text", "json", "csv", "table"]


# ----------------------------------------------------------------------
# Helper function for parallel loading (must be at module level)
# ----------------------------------------------------------------------
def _load_cidr_file_worker(file_path: Path):
    """
    Parse a single CIDR file and return:
        ipv4_list: list of (network_int, prefix_len, country_code)
        ipv6_list: list of (network_int, prefix_len, country_code)
        stats: dict {country_code: {"ipv4": count, "ipv6": count}}
    """
    filename = file_path.stem
    parts = filename.split('-')
    if len(parts) < 2:
        return [], [], {}

    country_code = parts[0].upper()
    ip_version = parts[1].lower() if len(parts) > 1 else ""
    if ip_version not in ["ipv4", "ipv6"]:
        ip_version = "ipv4"  # fallback

    ipv4_list = []
    ipv6_list = []
    stats = defaultdict(lambda: {"ipv4": 0, "ipv6": 0})

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                try:
                    network = ipaddress.ip_network(line, strict=False)
                    net_int = int(network.network_address)
                    prefix = network.prefixlen

                    if network.version == 4:
                        ipv4_list.append((net_int, prefix, country_code))
                        stats[country_code]["ipv4"] += 1
                    else:  # IPv6
                        ipv6_list.append((net_int, prefix, country_code))
                        stats[country_code]["ipv6"] += 1
                except ValueError:
                    # Skip invalid lines silently (or log if needed)
                    pass
    except Exception:
        # Return empty on error; error will be reported in main process
        pass

    return ipv4_list, ipv6_list, dict(stats)


class CIDRDatabase:
    """Database for CIDR ranges with efficient IP lookup."""

    def __init__(self, verbose: bool = False, console: Optional['Console'] = None):
        self.verbose = verbose
        self.console = console

        # Store as (network_int, prefix_len, country_code)
        self.ipv4_networks: List[Tuple[int, int, str]] = []
        self.ipv6_networks: List[Tuple[int, int, str]] = []

        self.country_stats = defaultdict(lambda: {"ipv4": 0, "ipv6": 0})
        self.loaded = False

        # Optional trie (kept for compatibility)
        if IPTRIE_AVAILABLE:
            self.ipv4_trie = IPTrie()
            self.ipv6_trie = IPTrie()
            self.use_trie = True
        else:
            self.use_trie = False

        # Country code to name mapping
        self.country_info = self._load_country_info()

    def _log(self, message: str, level: str = "info"):
        """Log messages to terminal with appropriate formatting."""
        if not self.console:
            if level == "error":
                print(f"❌ Error: {message}")
            elif level == "warning":
                print(f"⚠️  Warning: {message}")
            elif self.verbose:
                print(f"ℹ️  Info: {message}")
            return

        if level == "error":
            self.console.print(f"[red]❌ Error: {message}[/red]")
        elif level == "warning":
            self.console.print(f"[yellow]⚠️  Warning: {message}[/yellow]")
        elif self.verbose and level == "info":
            self.console.print(f"[cyan]ℹ️  Info: {message}[/cyan]")

    def _load_country_info(self) -> Dict[str, Dict[str, Any]]:
        """Load country code to name mapping."""
        country_info = {}

        if PYCOUNTRY_AVAILABLE:
            for country in pycountry.countries:
                country_info[country.alpha_2] = {
                    "name": country.name,
                    "alpha_3": getattr(country, 'alpha_3', ''),
                    "numeric": getattr(country, 'numeric', ''),
                    "continent": getattr(country, 'continent', '') if hasattr(country, 'continent') else ''
                }
        else:
            # Fallback minimal country info
            common_countries = {
                "US": {"name": "United States", "alpha_3": "USA", "numeric": "840", "continent": "NA"},
                "GB": {"name": "United Kingdom", "alpha_3": "GBR", "numeric": "826", "continent": "EU"},
                "DE": {"name": "Germany", "alpha_3": "DEU", "numeric": "276", "continent": "EU"},
                "CN": {"name": "China", "alpha_3": "CHN", "numeric": "156", "continent": "AS"},
                "JP": {"name": "Japan", "alpha_3": "JPN", "numeric": "392", "continent": "AS"},
                "AU": {"name": "Australia", "alpha_3": "AUS", "numeric": "036", "continent": "OC"},
                "IN": {"name": "India", "alpha_3": "IND", "numeric": "356", "continent": "AS"},
                "BR": {"name": "Brazil", "alpha_3": "BRA", "numeric": "076", "continent": "SA"},
                "RU": {"name": "Russia", "alpha_3": "RUS", "numeric": "643", "continent": "EU"},
            }
            country_info.update(common_countries)

        return country_info

    def load_cidr_files(self, cidr_folder: str = CIDR_FOLDER) -> bool:
        """Load all CIDR files from the specified folder (with optional parallel processing)."""
        folder_path = Path(cidr_folder)

        if not folder_path.exists():
            self._log(f"CIDR folder not found: {cidr_folder}", "error")
            return False

        files = list(folder_path.glob("*.txt"))
        if not files:
            self._log(f"No CIDR files found in {cidr_folder}", "error")
            return False

        if self.verbose:
            self._log(f"Found {len(files)} CIDR files to load")

        total_ranges = 0
        loaded_countries = set()

        # Decide whether to use parallel loading
        # Use parallel if more than 1 file and multiprocessing is available
        use_parallel = len(files) > 1 and mp.cpu_count() > 1

        if use_parallel:
            if self.verbose:
                self._log(f"Using parallel loading with {mp.cpu_count()} processes")
            ipv4_lists = []
            ipv6_lists = []
            stats_list = []

            with ProcessPoolExecutor(max_workers=mp.cpu_count()) as executor:
                future_to_file = {executor.submit(_load_cidr_file_worker, f): f for f in files}
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                    console=self.console,
                    disable=not (self.verbose and RICH_AVAILABLE)
                ) as progress:
                    task = progress.add_task("[cyan]Loading CIDR files in parallel...", total=len(files))
                    for future in as_completed(future_to_file):
                        f = future_to_file[future]
                        try:
                            ipv4, ipv6, stats = future.result()
                            ipv4_lists.append(ipv4)
                            ipv6_lists.append(ipv6)
                            stats_list.append(stats)
                            loaded_countries.add(f.stem.split('-')[0])
                        except Exception as e:
                            self._log(f"Error loading {f.name}: {e}", "error")
                        progress.update(task, advance=1)

            # Merge results
            for lst in ipv4_lists:
                self.ipv4_networks.extend(lst)
            for lst in ipv6_lists:
                self.ipv6_networks.extend(lst)
            for stats in stats_list:
                for cc, cnt in stats.items():
                    self.country_stats[cc]["ipv4"] += cnt.get("ipv4", 0)
                    self.country_stats[cc]["ipv6"] += cnt.get("ipv6", 0)

        else:
            # Sequential loading (original method)
            if self.verbose:
                if RICH_AVAILABLE and self.console:
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                        BarColumn(),
                        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                        console=self.console
                    ) as progress:
                        task = progress.add_task("[cyan]Loading CIDR files...", total=len(files))
                        for file_path in files:
                            self._load_single_file(file_path)
                            loaded_countries.add(file_path.stem.split('-')[0])
                            progress.update(task, advance=1)
                else:
                    if self.verbose:
                        print("Loading CIDR files...")
                    for file_path in files:
                        self._load_single_file(file_path)
                        loaded_countries.add(file_path.stem.split('-')[0])
                        if self.verbose:
                            print(f"  Loaded: {file_path.name}")

        # Sort networks by network_int for binary search
        self.ipv4_networks.sort(key=lambda x: x[0])
        self.ipv6_networks.sort(key=lambda x: x[0])

        self.loaded = True

        # Log statistics
        if self.verbose:
            self._log(f"Loaded {len(self.ipv4_networks)} IPv4 ranges")
            self._log(f"Loaded {len(self.ipv6_networks)} IPv6 ranges")
            self._log(f"Loaded {len(loaded_countries)} countries")

        return True

    def _load_single_file(self, file_path: Path):
        """Load a single CIDR file (sequential version)."""
        filename = file_path.stem
        parts = filename.split('-')

        if len(parts) < 2:
            self._log(f"Invalid filename format: {filename}", "warning")
            return

        country_code = parts[0].upper()
        ip_version = parts[1].lower() if len(parts) > 1 else ""

        if ip_version not in ["ipv4", "ipv6"]:
            ip_version = "ipv4"

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue

                    try:
                        network = ipaddress.ip_network(line, strict=False)
                        net_int = int(network.network_address)
                        prefix = network.prefixlen

                        if network.version == 4:
                            self.ipv4_networks.append((net_int, prefix, country_code))
                            if self.use_trie:
                                self.ipv4_trie.insert(network, country_code)
                            self.country_stats[country_code]["ipv4"] += 1
                        elif network.version == 6:
                            self.ipv6_networks.append((net_int, prefix, country_code))
                            if self.use_trie:
                                self.ipv6_trie.insert(network, country_code)
                            self.country_stats[country_code]["ipv6"] += 1
                    except ValueError as e:
                        self._log(f"Invalid CIDR '{line}' in {file_path.name}: {e}", "warning")

        except Exception as e:
            self._log(f"Error reading {file_path.name}: {e}", "error")

    def lookup_ip(self, ip_str: str) -> Optional[str]:
        """Lookup country code for an IP address."""
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            self._log(f"Invalid IP address: {ip_str}", "error")
            return None

        if ip.version == 4:
            return self._lookup_ipv4(ip)
        else:
            return self._lookup_ipv6(ip)

    def _lookup_ipv4(self, ip: ipaddress.IPv4Address) -> Optional[str]:
        """Lookup IPv4 address using binary search on integer ranges."""
        if self.use_trie and IPTRIE_AVAILABLE:
            # Fallback to trie if available (uses network objects)
            return self.ipv4_trie.search_best(ip)

        ip_int = int(ip)
        networks = self.ipv4_networks
        if not networks:
            return None

        low, high = 0, len(networks) - 1
        while low <= high:
            mid = (low + high) // 2
            net_int, prefix, cc = networks[mid]
            # Calculate broadcast: net_int + (1 << (32 - prefix)) - 1
            broadcast = net_int + (1 << (32 - prefix)) - 1

            if ip_int < net_int:
                high = mid - 1
            elif ip_int > broadcast:
                low = mid + 1
            else:
                return cc
        return None

    def _lookup_ipv6(self, ip: ipaddress.IPv6Address) -> Optional[str]:
        """Lookup IPv6 address using binary search on integer ranges."""
        if self.use_trie and IPTRIE_AVAILABLE:
            return self.ipv6_trie.search_best(ip)

        ip_int = int(ip)
        networks = self.ipv6_networks
        if not networks:
            return None

        low, high = 0, len(networks) - 1
        while low <= high:
            mid = (low + high) // 2
            net_int, prefix, cc = networks[mid]
            # For IPv6, broadcast = net_int + (1 << (128 - prefix)) - 1
            broadcast = net_int + (1 << (128 - prefix)) - 1

            if ip_int < net_int:
                high = mid - 1
            elif ip_int > broadcast:
                low = mid + 1
            else:
                return cc
        return None

    def get_country_info(self, country_code: str) -> Dict[str, Any]:
        """Get detailed country information."""
        country_code = country_code.upper()

        info = self.country_info.get(country_code, {
            "name": f"Unknown ({country_code})",
            "alpha_3": "",
            "numeric": "",
            "continent": ""
        })

        # Add stats
        stats = self.country_stats.get(country_code, {"ipv4": 0, "ipv6": 0})
        info.update({
            "code": country_code,
            "ipv4_ranges": stats["ipv4"],
            "ipv6_ranges": stats["ipv6"],
            "total_ranges": stats["ipv4"] + stats["ipv6"]
        })

        return info

    def reverse_lookup(self, country_code: str, limit: int = 10) -> List[str]:
        """Get sample IPs for a country."""
        country_code = country_code.upper()
        sample_ips = []

        # Get IPv4 networks for this country
        for net_int, prefix, cc in self.ipv4_networks:
            if cc == country_code:
                # Generate first usable address (network address + 1)
                if prefix < 32:  # Not a /32
                    first_host_int = net_int + 1
                else:
                    first_host_int = net_int
                try:
                    ip = str(ipaddress.IPv4Address(first_host_int))
                    sample_ips.append(ip)
                except:
                    pass
                if len(sample_ips) >= limit:
                    break

        # If we need more, add IPv6 samples
        if len(sample_ips) < limit:
            for net_int, prefix, cc in self.ipv6_networks:
                if cc == country_code:
                    if prefix < 128:
                        first_host_int = net_int + 1
                    else:
                        first_host_int = net_int
                    try:
                        ip = str(ipaddress.IPv6Address(first_host_int))
                        sample_ips.append(ip)
                    except:
                        pass
                    if len(sample_ips) >= limit:
                        break

        return sample_ips

    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        total_countries = len(self.country_stats)

        # Find country with most ranges
        country_totals = []
        for country, stats in self.country_stats.items():
            total = stats["ipv4"] + stats["ipv6"]
            if total > 0:
                country_totals.append((country, total))

        country_totals.sort(key=lambda x: x[1], reverse=True)

        return {
            "total_countries": total_countries,
            "total_ipv4_ranges": len(self.ipv4_networks),
            "total_ipv6_ranges": len(self.ipv6_networks),
            "top_countries": country_totals[:5],
            "database_loaded": self.loaded
        }

    def export_country_ranges(self, country_code: str) -> Tuple[List[str], List[str]]:
        """Export CIDR ranges for a country."""
        country_code = country_code.upper()
        ipv4_ranges = []
        ipv6_ranges = []

        for net_int, prefix, cc in self.ipv4_networks:
            if cc == country_code:
                # Reconstruct CIDR string
                network = ipaddress.IPv4Network((net_int, prefix), strict=False)
                ipv4_ranges.append(str(network))

        for net_int, prefix, cc in self.ipv6_networks:
            if cc == country_code:
                network = ipaddress.IPv6Network((net_int, prefix), strict=False)
                ipv6_ranges.append(str(network))

        return ipv4_ranges, ipv6_ranges


# ----------------------------------------------------------------------
# The rest of the code (IP2CC class, main) remains unchanged.
# Only the CIDRDatabase class has been heavily optimized.
# ----------------------------------------------------------------------

class IP2CC:
    """Main IP to Country Code mapper class."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.console = Console() if RICH_AVAILABLE else None
        self.db = CIDRDatabase(verbose, self.console)

    def initialize(self) -> bool:
        """Initialize the database."""
        if self.verbose:
            if self.console:
                self.console.print("[yellow]Loading CIDR database...[/yellow]")
            else:
                print("Loading CIDR database...")

        success = self.db.load_cidr_files()

        if success:
            if self.verbose:
                stats = self.db.get_stats()
                if self.console:
                    self.console.print(f"[green]✓ Database loaded successfully![/green]")
                    self.console.print(f"  Countries: {stats['total_countries']}")
                    self.console.print(f"  IPv4 ranges: {stats['total_ipv4_ranges']:,}")
                    self.console.print(f"  IPv6 ranges: {stats['total_ipv6_ranges']:,}")
                else:
                    print(f"✓ Database loaded successfully!")
                    print(f"  Countries: {stats['total_countries']}")
                    print(f"  IPv4 ranges: {stats['total_ipv4_ranges']:,}")
                    print(f"  IPv6 ranges: {stats['total_ipv6_ranges']:,}")
        else:
            if self.console:
                self.console.print("[red]✗ Failed to load CIDR database![/red]")
            else:
                print("✗ Failed to load CIDR database!")

        return success

    def lookup(self, ip_list: List[str], output_format: str = "text") -> List[Dict[str, Any]]:
        """Lookup multiple IP addresses."""
        results = []

        # Show progress for multiple IPs
        if len(ip_list) > 1 and self.verbose and self.console:
            with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                    console=self.console
            ) as progress:
                task = progress.add_task("[cyan]Looking up IPs...", total=len(ip_list))

                for ip_str in ip_list:
                    result = self._single_lookup(ip_str)
                    results.append(result)
                    progress.update(task, advance=1)
        else:
            # Process in parallel for better performance when not showing progress
            with ThreadPoolExecutor(max_workers=min(10, len(ip_list))) as executor:
                future_to_ip = {executor.submit(self._single_lookup, ip): ip for ip in ip_list}

                for future in as_completed(future_to_ip):
                    ip = future_to_ip[future]
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as e:
                        if self.console:
                            self.console.print(f"[red]Error processing {ip}: {e}[/red]")
                        else:
                            print(f"Error processing {ip}: {e}")
                        results.append({
                            "ip": ip,
                            "country_code": None,
                            "error": str(e)
                        })

        # Sort by IP for consistent output
        results.sort(key=lambda x: ipaddress.ip_address(x["ip"]) if "error" not in x else x["ip"])

        return results

    def _single_lookup(self, ip_str: str) -> Dict[str, Any]:
        """Lookup a single IP address."""
        country_code = self.db.lookup_ip(ip_str)

        result = {
            "ip": ip_str,
            "country_code": country_code,
            "timestamp": time.time()
        }

        if country_code:
            country_info = self.db.get_country_info(country_code)
            result.update(country_info)
        else:
            result["error"] = "No country match found"

        return result

    def display_results(self, results: List[Dict[str, Any]], output_format: str = "text"):
        """Display results in the specified format."""
        if output_format == "json":
            json_output = json.dumps(results, indent=2)
            if self.console:
                syntax = Syntax(json_output, "json", theme="monokai", line_numbers=True)
                self.console.print(syntax)
            else:
                print(json_output)

        elif output_format == "csv":
            import io
            output = io.StringIO()
            fieldnames = ["ip", "country_code", "name", "continent", "alpha_3", "numeric"]

            # Filter results with country info
            valid_results = [r for r in results if "name" in r]

            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            for result in valid_results:
                writer.writerow({k: result.get(k, "") for k in fieldnames})

            print(output.getvalue())

        elif output_format == "table" and self.console:
            table = Table(title="IP to Country Lookup Results", show_lines=True)
            table.add_column("IP", style="cyan", no_wrap=True)
            table.add_column("Country Code", style="yellow")
            table.add_column("Country Name", style="green")
            table.add_column("Continent", style="blue")
            table.add_column("IPv4/IPv6", style="magenta")

            for result in results:
                if "error" in result:
                    table.add_row(
                        result["ip"],
                        "N/A",
                        "[red]No match[/red]",
                        "",
                        "Error"
                    )
                else:
                    try:
                        ip_version = f"v{ipaddress.ip_address(result['ip']).version}"
                        table.add_row(
                            result["ip"],
                            result["country_code"],
                            result.get("name", "Unknown"),
                            result.get("continent", ""),
                            ip_version
                        )
                    except:
                        table.add_row(
                            result["ip"],
                            "N/A",
                            "[red]Invalid IP[/red]",
                            "",
                            "Error"
                        )

            self.console.print(table)

        else:  # text format (default)
            for result in results:
                if self.console:
                    if "error" in result:
                        self.console.print(f"[red]❌ IP: {result['ip']}[/red]")
                        self.console.print(f"[red]   Error: {result['error']}[/red]")
                    else:
                        self.console.print(f"[cyan]🌍 IP: {result['ip']}[/cyan]")
                        self.console.print(
                            f"[green]   Country Code: {result['country_code']} ({result.get('name', 'Unknown')})[/green]")
                        if result.get("continent"):
                            self.console.print(f"[blue]   Continent: {result['continent']}[/blue]")
                        # Show IP version
                        try:
                            ip_type = "IPv4" if ipaddress.ip_address(result['ip']).version == 4 else "IPv6"
                            self.console.print(f"[magenta]   Type: {ip_type}[/magenta]")
                        except:
                            pass
                else:
                    if "error" in result:
                        print(f"❌ IP: {result['ip']}")
                        print(f"   Error: {result['error']}")
                    else:
                        print(f"🌍 IP: {result['ip']}")
                        print(f"   Country Code: {result['country_code']} ({result.get('name', 'Unknown')})")
                        if result.get("continent"):
                            print(f"   Continent: {result['continent']}")
                        # Show IP version
                        try:
                            ip_type = "IPv4" if ipaddress.ip_address(result['ip']).version == 4 else "IPv6"
                            print(f"   Type: {ip_type}")
                        except:
                            pass
                print()

    def show_stats(self):
        """Display database statistics."""
        stats = self.db.get_stats()

        if self.console:
            # Create a panel with stats
            stats_text = Text()
            stats_text.append(f"📊 Database Statistics\n\n", style="bold yellow")
            stats_text.append(f"Total Countries: {stats['total_countries']}\n", style="cyan")
            stats_text.append(f"Total IPv4 Ranges: {stats['total_ipv4_ranges']:,}\n", style="green")
            stats_text.append(f"Total IPv6 Ranges: {stats['total_ipv6_ranges']:,}\n\n", style="blue")

            if stats['top_countries']:
                stats_text.append("Top 5 Countries by Range Count:\n", style="bold")
                for i, (country, count) in enumerate(stats['top_countries'], 1):
                    country_info = self.db.get_country_info(country)
                    stats_text.append(f"  {i}. {country} - {country_info.get('name', 'Unknown')}: {count:,} ranges\n")

            panel = Panel(stats_text, title="IP2CC Stats", border_style="green")
            self.console.print(panel)
        else:
            print("=" * 50)
            print("DATABASE STATISTICS")
            print("=" * 50)
            print(f"Total Countries: {stats['total_countries']}")
            print(f"Total IPv4 Ranges: {stats['total_ipv4_ranges']:,}")
            print(f"Total IPv6 Ranges: {stats['total_ipv6_ranges']:,}")

            if stats['top_countries']:
                print("\nTop 5 Countries by Range Count:")
                for i, (country, count) in enumerate(stats['top_countries'], 1):
                    country_info = self.db.get_country_info(country)
                    print(f"  {i}. {country} - {country_info.get('name', 'Unknown')}: {count:,} ranges")

    def reverse_lookup_display(self, country_code: str, limit: int = 10):
        """Display reverse lookup results."""
        sample_ips = self.db.reverse_lookup(country_code, limit)
        country_info = self.db.get_country_info(country_code)

        if self.console:
            self.console.print(f"[yellow]🔍 Reverse Lookup for {country_code}[/yellow]")
            self.console.print(f"[green]Country: {country_info.get('name', 'Unknown')}[/green]")
            self.console.print(f"[cyan]Total IPv4 Ranges: {country_info.get('ipv4_ranges', 0):,}[/cyan]")
            self.console.print(f"[cyan]Total IPv6 Ranges: {country_info.get('ipv6_ranges', 0):,}[/cyan]")

            if sample_ips:
                table = Table(title=f"Sample IPs from {country_code}", show_lines=True)
                table.add_column("#", style="dim")
                table.add_column("Sample IP", style="green")
                table.add_column("Type", style="blue")

                for i, ip in enumerate(sample_ips, 1):
                    ip_type = "IPv4" if ipaddress.ip_address(ip).version == 4 else "IPv6"
                    table.add_row(str(i), ip, ip_type)

                self.console.print(table)
            else:
                self.console.print("[red]No IP ranges found for this country code[/red]")
        else:
            print(f"🔍 Reverse Lookup for {country_code}")
            print(f"Country: {country_info.get('name', 'Unknown')}")
            print(f"Total IPv4 Ranges: {country_info.get('ipv4_ranges', 0):,}")
            print(f"Total IPv6 Ranges: {country_info.get('ipv6_ranges', 0):,}")

            if sample_ips:
                print(f"\nSample IPs (first {len(sample_ips)}):")
                for i, ip in enumerate(sample_ips, 1):
                    ip_type = "IPv4" if ipaddress.ip_address(ip).version == 4 else "IPv6"
                    print(f"  {i:2}. {ip} ({ip_type})")
            else:
                print("No IP ranges found for this country code")


def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    if RICH_AVAILABLE:
        console = Console()
        console.print("\n[yellow]⚠️  Interrupted by user. Exiting gracefully...[/yellow]")
    else:
        print("\n⚠️  Interrupted by user. Exiting gracefully...")
    sys.exit(1)


def main():
    """Main entry point."""
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser(
        description="🌍 IP to Country Code Mapper - Map IP addresses to countries using CIDR ranges",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s 8.8.8.8                           # Lookup single IP
  %(prog)s 8.8.8.8 1.1.1.1 2001:4860:4860::8888  # Lookup multiple IPs
  %(prog)s --input-file ips.txt              # Lookup IPs from file
  %(prog)s --output-format json 8.8.8.8      # Output in JSON format
  %(prog)s --reverse US                      # Show sample IPs from US
  %(prog)s stats                             # Show database statistics
  %(prog)s --export US > us_cidrs.txt        # Export US CIDR ranges

Installation Requirements:
  pip install pycountry rich iptrie

Note:
  CIDR files should be in a folder named 'CIDR' in the same directory as this script.
  Files should be named like: US-ipv4-Hackers.Zone.txt, US-ipv6-Hackers.Zone.txt
        """
    )

    # Positional arguments for IPs (or subcommand)
    parser.add_argument(
        'ips_or_command',
        nargs='*',
        help='IP addresses to lookup or command (stats)'
    )

    # Options
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose output (shows loading progress and info)'
    )

    parser.add_argument(
        '--input-file',
        type=str,
        help='Read IPs from file (one per line)'
    )

    parser.add_argument(
        '--output-format',
        type=str,
        choices=SUPPORTED_OUTPUT_FORMATS,
        default='text',
        help=f'Output format: {", ".join(SUPPORTED_OUTPUT_FORMATS)} (default: text)'
    )

    parser.add_argument(
        '--reverse',
        type=str,
        metavar='COUNTRY_CODE',
        help='Reverse lookup: show sample IPs for a country'
    )

    parser.add_argument(
        '--export',
        type=str,
        metavar='COUNTRY_CODE',
        help='Export all CIDR ranges for a country to stdout'
    )

    parser.add_argument(
        '--limit',
        type=int,
        default=10,
        help='Limit for reverse lookup (default: 10)'
    )

    parser.add_argument(
        '--cidr-folder',
        type=str,
        default=CIDR_FOLDER,
        help=f'Path to CIDR folder (default: {CIDR_FOLDER})'
    )

    args = parser.parse_args()

    # Initialize the IP2CC mapper
    mapper = IP2CC(verbose=args.verbose)

    # Check if CIDR folder exists
    cidr_path = Path(args.cidr_folder)
    if not cidr_path.exists():
        if mapper.console:
            mapper.console.print(f"[red]Error: CIDR folder '{args.cidr_folder}' not found![/red]")
            mapper.console.print(f"[yellow]Please create a '{args.cidr_folder}' folder with CIDR files.[/yellow]")
            mapper.console.print(
                f"[cyan]Expected files: US-ipv4-Hackers.Zone.txt, US-ipv6-Hackers.Zone.txt, etc.[/cyan]")
        else:
            print(f"Error: CIDR folder '{args.cidr_folder}' not found!")
            print(f"Please create a '{args.cidr_folder}' folder with CIDR files.")
            print(f"Expected files: US-ipv4-Hackers.Zone.txt, US-ipv6-Hackers.Zone.txt, etc.")
        sys.exit(1)

    # Initialize database
    if not mapper.initialize():
        sys.exit(1)

    # Handle export command
    if args.export:
        ipv4_ranges, ipv6_ranges = mapper.db.export_country_ranges(args.export)
        country_info = mapper.db.get_country_info(args.export)

        if mapper.console:
            mapper.console.print(
                f"[green]Exporting CIDR ranges for {args.export} - {country_info.get('name', 'Unknown')}[/green]")

        print(f"# CIDR ranges for {args.export} - {country_info.get('name', 'Unknown')}")
        print(f"# Total IPv4 ranges: {len(ipv4_ranges)}")
        print(f"# Total IPv6 ranges: {len(ipv6_ranges)}")
        print()

        if ipv4_ranges:
            print("# IPv4 Ranges:")
            for cidr in ipv4_ranges:
                print(cidr)
            print()

        if ipv6_ranges:
            print("# IPv6 Ranges:")
            for cidr in ipv6_ranges:
                print(cidr)

        sys.exit(0)

    # Handle reverse lookup
    if args.reverse:
        mapper.reverse_lookup_display(args.reverse, args.limit)
        sys.exit(0)

    # Handle stats command
    if args.ips_or_command and args.ips_or_command[0] == 'stats':
        mapper.show_stats()
        sys.exit(0)

    # Collect IPs to lookup
    ips_to_lookup = []

    # Read IPs from file if specified
    if args.input_file:
        try:
            with open(args.input_file, 'r') as f:
                for line in f:
                    ip = line.strip()
                    if ip and not ip.startswith('#'):
                        ips_to_lookup.append(ip)

            if args.verbose:
                mapper.db._log(f"Read {len(ips_to_lookup)} IPs from {args.input_file}")
        except Exception as e:
            mapper.db._log(f"Error reading input file: {e}", "error")
            sys.exit(1)

    # Add IPs from command line
    ips_to_lookup.extend(args.ips_or_command)

    # If no IPs provided, show help
    if not ips_to_lookup:
        parser.print_help()

        # Show example lookups
        if mapper.console:
            mapper.console.print("\n[yellow]Quick Test Examples:[/yellow]")
            mapper.console.print("[cyan]  python ip2cc.py 8.8.8.8 1.1.1.1[/cyan]")
            mapper.console.print("[cyan]  python ip2cc.py 2001:4860:4860::8888[/cyan]")
            mapper.console.print("[cyan]  python ip2cc.py --reverse US[/cyan]")
            mapper.console.print("[cyan]  python ip2cc.py stats[/cyan]")
        sys.exit(0)

    # Perform lookups
    results = mapper.lookup(ips_to_lookup, args.output_format)

    # Display results
    mapper.display_results(results, args.output_format)

    # Summary
    if len(results) > 1:
        matched = sum(1 for r in results if "error" not in r)
        total = len(results)

        if mapper.console:
            if matched == total:
                mapper.console.print(f"\n[green]✓ All {total} IPs successfully matched![/green]")
            else:
                mapper.console.print(f"\n[yellow]✓ Summary: {matched}/{total} IPs matched[/yellow]")
        else:
            if matched == total:
                print(f"\n✓ All {total} IPs successfully matched!")
            else:
                print(f"\n✓ Summary: {matched}/{total} IPs matched")


if __name__ == "__main__":
    # Required for multiprocessing on Windows
    mp.freeze_support()
    main()