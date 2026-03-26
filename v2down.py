#!/usr/bin/env python3
"""
v2down.py - by Mmdre
Requirements:
pip install rich httpx tenacity
Usage:
python v2down.py --input urls.txt --workers 4 --output-dir downloads/
"""
import argparse
import asyncio
import logging
import os
import random
import re
import signal
import sys
import time
import urllib.robotparser
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional, Set
import httpx
from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.progress import (
Progress,
SpinnerColumn,
TextColumn,
BarColumn,
DownloadColumn,
TransferSpeedColumn,
TimeRemainingColumn,
TaskProgressColumn,
MofNCompleteColumn,
)
from rich.table import Table
from rich.text import Text
from tenacity import (
retry,
stop_after_attempt,
wait_exponential,
retry_if_exception_type,
before_sleep_log,
)
__version__ = "1.0.0"
class DownloadStatus(Enum):
PENDING = ("⏳", "dim")
DOWNLOADING = ("🔽", "cyan")
SUCCESS = ("✅", "green")
RETRYING = ("🔄", "yellow")
SKIPPED = ("⏭️", "blue")
FAILED = ("❌", "red")
BLOCKED = ("🚫", "magenta")
def __init__(self, emoji, color):
self.emoji = emoji
self.color = color
@dataclass
class DownloadResult:
url: str
index: int
status: DownloadStatus
filename: Optional[str] = None
size: int = 0
duration: float = 0.0
download_time: float = 0.0  # Time spent actually downloading
error: Optional[str] = None
status_code: Optional[int] = None
content_type: Optional[str] = None
attempt: int = 1
retry_after: float = 0.0
progress: float = 0.0
speed: float = 0.0
last_update: float = field(default_factory=time.time)
download_start_time: Optional[float] = None
class DownloadDisplay:
"""Manages the display of download status with downloading items at bottom."""
def __init__(self, console: Console, max_visible: int = 8, max_workers: int = 1):
self.console = console
self.max_visible = max_visible
self.max_workers = max_workers
self.results: Dict[int, DownloadResult] = {}
self.downloading_indices: Set[int] = set()  # Only truly downloading items
self.recent_indices: deque = deque(maxlen=50)  # Recently completed/failed
self.other_indices: Set[int] = set()  # Other non-downloading items
def update_result(self, result: DownloadResult):
"""Update a result and manage visibility."""
self.results[result.index] = result
if result.status == DownloadStatus.DOWNLOADING:
# Add to downloading set
self.downloading_indices.add(result.index)
# Remove from other/recent if it was there
if result.index in self.recent_indices:
self.recent_indices.remove(result.index)
if result.index in self.other_indices:
self.other_indices.remove(result.index)
elif result.status in [DownloadStatus.SUCCESS, DownloadStatus.FAILED, DownloadStatus.BLOCKED]:
# Remove from downloading if it was there
if result.index in self.downloading_indices:
self.downloading_indices.remove(result.index)
# Add to recent (most recent at the end)
if result.index in self.recent_indices:
self.recent_indices.remove(result.index)
self.recent_indices.append(result.index)
if result.index in self.other_indices:
self.other_indices.remove(result.index)
elif result.status == DownloadStatus.RETRYING:
# Remove from downloading (if it was there)
if result.index in self.downloading_indices:
self.downloading_indices.remove(result.index)
# Add to other
if result.index not in self.recent_indices:
self.other_indices.add(result.index)
elif result.status == DownloadStatus.SKIPPED:
# Remove from downloading if it was there
if result.index in self.downloading_indices:
self.downloading_indices.remove(result.index)
# Add to recent
if result.index in self.recent_indices:
self.recent_indices.remove(result.index)
self.recent_indices.append(result.index)
if result.index in self.other_indices:
self.other_indices.remove(result.index)
elif result.status == DownloadStatus.PENDING:
# Add to other
if (result.index not in self.recent_indices and
result.index not in self.downloading_indices):
self.other_indices.add(result.index)
def render(self, phase: str, total_urls: int) -> Group:
"""Render the current display with downloading items at bottom."""
# Overall progress
total = len(self.results)
success = sum(1 for r in self.results.values() if r.status == DownloadStatus.SUCCESS)
failed = sum(1 for r in self.results.values() if r.status == DownloadStatus.FAILED)
downloading = sum(1 for r in self.results.values() if r.status == DownloadStatus.DOWNLOADING)
retrying = sum(1 for r in self.results.values() if r.status == DownloadStatus.RETRYING)
# Header
header = Text(f"📥 {phase} | Total: {total_urls} | ", style="bold cyan")
header.append(f"✅ {success} ", style="green")
header.append(f"❌ {failed} ", style="red" if failed > 0 else "dim")
header.append(f"🔽 {downloading}/{self.max_workers} ", style="cyan" if downloading > 0 else "dim")
if retrying > 0:
header.append(f"🔄 {retrying} ", style="yellow")
# Create table for visible items
table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), expand=False)
table.add_column("Status", width=3)
table.add_column("ID", width=5)
table.add_column("Progress", width=15)
table.add_column("URL", width=50)
table.add_column("Info", width=30)
# Collect all indices that should be visible
display_indices = []
# Step 1: Add other items (non-downloading, non-recent) at the TOP
# Sort by last_update (newest first) so newest appear at the bottom of this section
other_list = sorted(self.other_indices,
key=lambda x: self.results[x].last_update if x in self.results else 0)
# Step 2: Add recent items (completed/failed/skipped) - these go ABOVE the downloading section
# We want newest recent items at the bottom of the recent section (just above downloading)
recent_list = list(self.recent_indices)  # Already in order of addition (oldest first)
# Step 3: Add downloading items at the VERY BOTTOM
downloading_list = sorted(self.downloading_indices)
# Calculate how many slots we have for non-downloading items
downloading_slots = min(len(downloading_list), self.max_workers)
non_downloading_slots = self.max_visible - downloading_slots
# We want to fill the display from top to bottom:
# 1. Other items (oldest at top, newest at bottom of this section)
# 2. Recent items (oldest at top, newest at bottom of this section)
# 3. Downloading items (at the very bottom)
# Combine other and recent for display
all_non_downloading = list(other_list) + list(recent_list)
# We want to show the most recent non-downloading items
# Since we want newest at bottom, we take the last N from the combined list
# where N = non_downloading_slots
if len(all_non_downloading) > non_downloading_slots:
# Take the most recent (which are at the end of the list)
non_downloading_display = all_non_downloading[-non_downloading_slots:]
else:
non_downloading_display = all_non_downloading
# Add non-downloading items first (these go at the top)
display_indices.extend(non_downloading_display)
# Add downloading items at the bottom (limit to max_workers)
for idx in downloading_list[:self.max_workers]:
display_indices.append(idx)
# Add rows for display indices (from top to bottom)
for idx in display_indices:
if idx not in self.results:
continue
result = self.results[idx]
# Truncate URL for display
url_display = result.url
if len(url_display) > 45:
url_display = url_display[:20] + "..." + url_display[-20:]
# Status column
status_text = Text(result.status.emoji, style=result.status.color)
# Progress column
if result.status == DownloadStatus.DOWNLOADING:
progress_bar = "━" * int(result.progress * 10)
progress_text = Text(f"[{progress_bar:<10}]", style="cyan")
elif result.status == DownloadStatus.RETRYING:
progress_text = Text(f"Retry {result.attempt}", style="yellow")
else:
progress_text = Text("", style="dim")
# Info column
if result.status == DownloadStatus.SUCCESS:
# Format size
if result.size < 1024:
size_text = f"{result.size} B"
elif result.size < 1024 * 1024:
size_text = f"{result.size / 1024:.1f} KiB"
else:
size_text = f"{result.size / (1024 * 1024):.1f} MiB"
# Use download_time instead of total duration
time_text = f"({result.download_time:.1f}s)" if result.download_time > 0 else ""
info_text = Text(f"{size_text} .txt {time_text}", style="green")
elif result.status == DownloadStatus.FAILED:
info_text = Text(f"{result.error or 'Error'}", style="red")
elif result.status == DownloadStatus.BLOCKED:
info_text = Text("robots.txt", style="magenta")
elif result.status == DownloadStatus.SKIPPED:
info_text = Text("Skipped", style="blue")
elif result.status == DownloadStatus.RETRYING:
if result.retry_after > 0:
info_text = Text(f"Retry in {result.retry_after:.1f}s", style="yellow")
else:
info_text = Text(f"Retry {result.attempt}", style="yellow")
else:
info_text = Text("", style="dim")
table.add_row(
status_text,
Text(f"{idx:04d}", style="dim"),
progress_text,
Text(url_display, style="dim" if result.status == DownloadStatus.SKIPPED else ""),
info_text
)
# Add placeholder if we have fewer than max visible
for _ in range(self.max_visible - len(display_indices)):
table.add_row(
Text("", style="dim"),
Text("", style="dim"),
Text("", style="dim"),
Text("", style="dim"),
Text("", style="dim")
)
return Group(header, table)
class GracefulExit(SystemExit):
"""Custom exception for graceful exit."""
code = 0
class Downloader:
def __init__(self, args):
self.args = args
self.urls: List[str] = []
self.results: List[DownloadResult] = []
self.robot_parser = None
self.client = None
self.failed_indices = []
self.attempt_counts = defaultdict(int)
self.display = DownloadDisplay(console, max_visible=args.max_visible,
max_workers=args.workers if args.workers > 0 else 1)
self._partial_files: Set[Path] = set()  # Track partial files for cleanup
self._is_interrupted = False
self._shutdown_event = asyncio.Event()
self._active_downloads: Set[int] = set()  # Track truly active downloads
# Setup logging
self.logger = logging.getLogger("v2down")
if args.log_file:
handler = logging.FileHandler(args.log_file, mode='a', encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
self.logger.addHandler(handler)
self.logger.setLevel(logging.INFO)
# Create output directory
self.output_dir = Path(args.output_dir)
self.output_dir.mkdir(parents=True, exist_ok=True)
def _handle_shutdown_signal(self, signum):
"""Handle shutdown signals gracefully."""
if self._is_interrupted:
# Second Ctrl+C, force exit
console.print("
[red]Force quitting...[/red]")
raise KeyboardInterrupt
self._is_interrupted = True
console.print("
[yellow]⚠️  Shutting down gracefully... Press Ctrl+C again to force quit.[/yellow]")
# Set shutdown event to trigger cleanup
self._shutdown_event.set()
def cleanup_partial_files(self):
"""Clean up all partial files."""
if not self._partial_files:
return
removed = 0
for partial_path in self._partial_files:
try:
if partial_path.exists():
partial_path.unlink()
removed += 1
self.logger.info(f"Cleaned up partial file: {partial_path.name}")
except Exception as e:
self.logger.error(f"Failed to remove {partial_path}: {e}")
if removed:
console.print(f"[dim]Cleaned up {removed} partial file(s)[/dim]")
self._partial_files.clear()
def _register_partial_file(self, path: Path):
"""Register a partial file for cleanup."""
self._partial_files.add(path)
def _unregister_partial_file(self, path: Path):
"""Unregister a partial file (when download completes)."""
if path in self._partial_files:
self._partial_files.remove(path)
def parse_url_file(self, filename: str) -> List[str]:
"""Parse URLs from file, skipping comments and blanks."""
if not Path(filename).exists():
raise FileNotFoundError(f"Input file not found: {filename}")
urls = []
with open(filename, 'r', encoding='utf-8') as f:
for line in f:
line = line.strip()
if not line or line.startswith('#'):
continue
urls.append(line)
return urls
def get_filename(self, result: DownloadResult, content_disposition: Optional[str] = None) -> str:
"""Generate filename - always use .txt extension."""
return f"{result.index:04d}.txt"
def can_fetch(self, url: str) -> bool:
"""Check robots.txt if enabled."""
if not self.args.respect_robots:
return True
if not self.robot_parser:
return True
try:
from urllib.parse import urlparse
parsed = urlparse(url)
return self.robot_parser.can_fetch("*", url)
except:
return True  # If we can't parse, allow fetch
def setup_robots_txt(self, urls: List[str]) -> None:
"""Setup robot parser for domains in URLs."""
if not self.args.respect_robots:
return
from urllib.parse import urlparse
domains = {}
for url in urls:
try:
parsed = urlparse(url)
if parsed.netloc:
domains[(parsed.scheme, parsed.netloc)] = True
except:
continue
self.robot_parser = urllib.robotparser.RobotFileParser()
for scheme, domain in domains:
try:
robots_url = f"{scheme}://{domain}/robots.txt"
self.robot_parser.set_url(robots_url)
# Read with timeout
self.robot_parser.read()
except Exception as e:
self.logger.debug(f"Could not read robots.txt from {domain}: {e}")
async def download_single(self, url: str, index: int, semaphore) -> Optional[DownloadResult]:
"""Download a single URL with retry logic."""
# Check for shutdown
if self._is_interrupted:
return None
result = DownloadResult(url=url, index=index, status=DownloadStatus.PENDING)
self.attempt_counts[index] += 1
attempt = self.attempt_counts[index]
# Update display
self.display.update_result(result)
# Check robots.txt
if not self.can_fetch(url):
result.status = DownloadStatus.BLOCKED
result.error = "Blocked by robots.txt"
result.last_update = time.time()
self.display.update_result(result)
self.logger.warning(f"Blocked by robots.txt: {url}")
return result
# Check if final file already exists
final_path = self.output_dir / f"{index:04d}.txt"
if self.args.skip_existing and final_path.exists():
result.status = DownloadStatus.SKIPPED
result.error = "File already exists"
result.filename = final_path.name
result.last_update = time.time()
self.display.update_result(result)
return result
# Check for partial file
partial_path = self.output_dir / f"{index:04d}.partial"
resume_from = 0
if partial_path.exists():
resume_from = partial_path.stat().st_size
result.progress = min(0.95, resume_from / max(1, resume_from * 2))  # Estimate
# Register partial file for cleanup
self._register_partial_file(partial_path)
# Prepare headers
headers = {}
for h in self.args.header:
if ':' in h:
key, val = h.split(':', 1)
headers[key.strip()] = val.strip()
if resume_from > 0:
headers['Range'] = f'bytes={resume_from}-'
if 'User-Agent' not in headers:
headers['User-Agent'] = 'Mozilla/5.0 (compatible; v2down/1.0.0; +https://github.com/example/v2down)'
# Exponential backoff with jitter for retries
if attempt > 1:
base_delay = min(self.args.delay_max * 10,
self.args.delay_min * (2.5 ** (attempt - 1)) + random.uniform(0, 2))
result.status = DownloadStatus.RETRYING
result.attempt = attempt
result.retry_after = base_delay
result.last_update = time.time()
self.display.update_result(result)
# Check for shutdown during delay
try:
await asyncio.wait_for(self._shutdown_event.wait(), timeout=base_delay)
if self._is_interrupted:
# Register partial file for cleanup before returning
if partial_path.exists():
self._register_partial_file(partial_path)
return None
except asyncio.TimeoutError:
pass  # Delay completed
# Check for shutdown before starting download
if self._is_interrupted:
if partial_path.exists():
self._register_partial_file(partial_path)
return None
# Acquire semaphore - this ensures we only have max_workers concurrent downloads
async with semaphore:
# Set status to DOWNLOADING only after acquiring semaphore
result.status = DownloadStatus.DOWNLOADING
result.last_update = time.time()
result.download_start_time = time.time()  # Start timing actual download
self.display.update_result(result)
start_time = time.time()
last_update_time = start_time
downloaded = resume_from
actual_download_time = 0.0
try:
# Small random delay between requests even in parallel
try:
delay = random.uniform(self.args.delay_min, self.args.delay_max)
await asyncio.wait_for(self._shutdown_event.wait(), timeout=delay)
if self._is_interrupted:
if partial_path.exists():
self._register_partial_file(partial_path)
return None
except asyncio.TimeoutError:
pass  # Delay completed
# Check for shutdown before making request
if self._is_interrupted:
if partial_path.exists():
self._register_partial_file(partial_path)
return None
async with self.client.stream(
'GET', url,
headers=headers,
timeout=self.args.timeout,
follow_redirects=True
) as response:
result.status_code = response.status_code
result.content_type = response.headers.get('content-type')
if response.status_code == 200 or (response.status_code == 206 and resume_from > 0):
# Always use .txt extension
filename = self.get_filename(result)
result.filename = filename
# Get total size for progress
total_size = int(response.headers.get('content-length', 0))
if resume_from > 0 and response.status_code == 206:
if 'content-range' in response.headers:
# Parse Content-Range: bytes 0-1000/1001
range_header = response.headers.get('content-range', '')
match = re.search(r'/(\d+)$', range_header)
if match:
total_size = int(match.group(1))
else:
total_size += resume_from
# Download to partial file
mode = 'ab' if resume_from > 0 else 'wb'
# Register partial file before starting download
self._register_partial_file(partial_path)
chunk_start_time = time.time()
with open(partial_path, mode) as f:
async for chunk in response.aiter_bytes():
# Check for shutdown during download
if self._is_interrupted:
break
f.write(chunk)
downloaded += len(chunk)
chunk_end_time = time.time()
actual_download_time += chunk_end_time - chunk_start_time
chunk_start_time = chunk_end_time
# Update progress periodically (not on every chunk)
current_time = time.time()
if current_time - last_update_time > 0.1:  # 10 FPS
if total_size > 0:
result.progress = downloaded / total_size
if actual_download_time > 0:
result.speed = downloaded / actual_download_time
result.last_update = current_time
self.display.update_result(result)
last_update_time = current_time
# Check if we were interrupted during download
if self._is_interrupted:
# Keep partial file registered for cleanup
return None
# Final progress update
result.progress = 1.0
result.size = downloaded
result.download_time = actual_download_time  # Store actual download time
# Rename on success
final_path = self.output_dir / filename
try:
partial_path.rename(final_path)
# Unregister partial file since it's now a complete file
self._unregister_partial_file(partial_path)
except OSError:
# Fallback: copy if rename fails (cross-device)
import shutil
shutil.move(str(partial_path), str(final_path))
if partial_path.exists():
partial_path.unlink()
result.status = DownloadStatus.SUCCESS
result.duration = time.time() - start_time  # Total time including delays
result.last_update = time.time()
self.display.update_result(result)
self.logger.info(
f"Success: {url} -> {filename} ({result.size} bytes, download time: {actual_download_time:.2f}s)")
else:
result.status = DownloadStatus.FAILED
result.error = f"HTTP {response.status_code}"
result.duration = time.time() - start_time
result.last_update = time.time()
# Don't delete partial file on failure, keep for resume
if partial_path.exists():
self._register_partial_file(partial_path)
self.display.update_result(result)
self.logger.error(f"HTTP {response.status_code}: {url}")
except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
result.status = DownloadStatus.FAILED
result.error = type(e).__name__.replace('Exception', '')
result.duration = time.time() - start_time
result.last_update = time.time()
# Don't delete partial file on network error, keep for resume
if partial_path.exists():
self._register_partial_file(partial_path)
self.display.update_result(result)
self.logger.error(f"Network error ({type(e).__name__}): {url}")
except Exception as e:
result.status = DownloadStatus.FAILED
result.error = type(e).__name__
result.duration = time.time() - start_time
result.last_update = time.time()
if partial_path.exists():
self._register_partial_file(partial_path)
self.display.update_result(result)
self.logger.error(f"Error ({type(e).__name__}): {url}")
return result
def print_summary(self):
"""Print final summary with statistics."""
console.print("
" + "=" * 60)
console.print("[bold cyan]📊 DOWNLOAD SUMMARY[/bold cyan]")
console.print("=" * 60)
total = len(self.urls)
success = sum(1 for r in self.results if r.status == DownloadStatus.SUCCESS)
skipped = sum(1 for r in self.results if r.status == DownloadStatus.SKIPPED)
blocked = sum(1 for r in self.results if r.status == DownloadStatus.BLOCKED)
failed = total - success - skipped - blocked
# Calculate total download time and size
total_download_time = sum(r.download_time for r in self.results if r.status == DownloadStatus.SUCCESS)
total_size = sum(r.size for r in self.results if r.status == DownloadStatus.SUCCESS)
# Categorize failures
failures = defaultdict(int)
for r in self.results:
if r.status == DownloadStatus.FAILED and r.error:
if 'HTTP 4' in r.error:
failures['HTTP 4xx (Client)'] += 1
elif 'HTTP 5' in r.error:
failures['HTTP 5xx (Server)'] += 1
elif 'Timeout' in r.error:
failures['Timeout'] += 1
elif any(x in r.error for x in ['Connect', 'Read']):
failures['Network Error'] += 1
else:
failures[r.error] += 1
# Create summary table
table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
table.add_column("Status", style="cyan", width=12)
table.add_column("Count", justify="right")
table.add_column("Percentage", justify="right")
table.add_column("Details", style="dim")
table.add_row("✅ Success", str(success), f"{success / total * 100:.1f}%",
f"Size: {total_size:,} bytes | Time: {total_download_time:.1f}s")
table.add_row("⏭️ Skipped", str(skipped), f"{skipped / total * 100:.1f}%" if total > 0 else "0%",
"File already existed")
table.add_row("🚫 Blocked", str(blocked), f"{blocked / total * 100:.1f}%" if total > 0 else "0%",
"by robots.txt")
table.add_row("❌ Failed", str(failed), f"{failed / total * 100:.1f}%" if total > 0 else "0%", "")
console.print(table)
if failures:
console.print("
[bold yellow]🔍 Failure Breakdown:[/bold yellow]")
fail_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
fail_table.add_column("Type", style="yellow")
fail_table.add_column("Count", justify="right")
for error_type, count in sorted(failures.items(), key=lambda x: x[1], reverse=True):
fail_table.add_row(error_type, str(count))
console.print(fail_table)
# List failed URLs (only if few)
failed_urls = [r for r in self.results if r.status == DownloadStatus.FAILED]
if failed_urls and len(failed_urls) <= 10:
console.print("
[bold red]❌ Failed URLs:[/bold red]")
for r in failed_urls[:5]:  # Show only first 5
console.print(f"  [{r.index:4d}] {r.url[:60]}...")
if r.error:
console.print(f"       → {r.error}")
if len(failed_urls) > 5:
console.print(f"  ... and {len(failed_urls) - 5} more")
elif failed_urls:
console.print(f"
[bold red]❌ {len(failed_urls)} URLs failed (use --log-file for details)[/bold red]")
async def run_phase(self, phase_name: str, indices: List[int], semaphore) -> List[DownloadResult]:
"""Run a download phase (initial or retry)."""
tasks = []
for idx in indices:
url = self.urls[idx - 1]  # URLs are 1-indexed in display
tasks.append(self.download_single(url, idx, semaphore))
# Run with live display
with Live(self.display.render(phase_name, len(self.urls)),
refresh_per_second=10, console=console, transient=False) as live:
# Update display periodically while tasks run
async def update_display():
while not self._is_interrupted:
live.update(self.display.render(phase_name, len(self.urls)))
await asyncio.sleep(0.1)
# Run display update and downloads concurrently
display_task = asyncio.create_task(update_display())
try:
results = await asyncio.gather(*tasks, return_exceptions=True)
except asyncio.CancelledError:
# Task was cancelled due to shutdown
display_task.cancel()
raise
finally:
display_task.cancel()
try:
await display_task
except asyncio.CancelledError:
pass
# Final display update
if not self._is_interrupted:
live.update(self.display.render(phase_name, len(self.urls)))
# Filter out exceptions and None results (from shutdown)
valid_results = []
for r in results:
if isinstance(r, DownloadResult):
valid_results.append(r)
elif r is None:
# Result was cancelled due to shutdown
pass
elif isinstance(r, Exception):
console.print(f"[red]Unexpected error: {r}[/red]")
return valid_results
async def run(self):
"""Main download process."""
# Setup signal handlers for Windows
if sys.platform == "win32":
# Windows doesn't support signal handlers the same way as Unix
# We'll handle Ctrl+C differently
pass
else:
# Setup signal handlers for Unix-like systems
loop = asyncio.get_running_loop()
for sig in (signal.SIGINT, signal.SIGTERM):
try:
loop.add_signal_handler(sig, self._handle_shutdown_signal, sig)
except NotImplementedError:
# Signal handlers not supported on this platform
pass
try:
# Read URLs
try:
self.urls = self.parse_url_file(self.args.input)
except FileNotFoundError as e:
console.print(f"[red]Error: {e}[/red]")
return 1
if not self.urls:
console.print("[yellow]No URLs found in input file.[/yellow]")
return 0
console.print(f"[green]Found {len(self.urls)} URLs to process[/green]")
# Setup robots.txt if needed
if self.args.respect_robots:
console.print("[cyan]🔍 Checking robots.txt for domains...[/cyan]")
self.setup_robots_txt(self.urls)
# Create HTTP client
limits = httpx.Limits(max_connections=self.args.workers * 2 if self.args.workers > 0 else 10)
async with httpx.AsyncClient(limits=limits) as self.client:
# Phase 1: Initial pass
console.print(f"
[bold cyan]⚡ Phase 1: Initial pass ({len(self.urls)} URLs) ⚡[/bold cyan]")
# Create semaphore for concurrency control
semaphore = asyncio.Semaphore(self.args.workers if self.args.workers > 0 else 1)
# Initial indices (all URLs)
indices = list(range(1, len(self.urls) + 1))
phase_results = await self.run_phase("Initial Download", indices, semaphore)
# Check for interruption
if self._is_interrupted:
raise GracefulExit()
# Update main results
for r in phase_results:
if r.index - 1 < len(self.results):
self.results[r.index - 1] = r
else:
self.results.append(r)
# Phase 2: Retry failed downloads
failed = [r.index for r in self.results if r.status == DownloadStatus.FAILED]
if failed and self.args.retries > 0 and not self._is_interrupted:
console.print(
f"
[bold yellow]🔄 Retrying {len(failed)} failed URLs (max {self.args.retries} retries) 🔄[/bold yellow]")
for retry_num in range(1, self.args.retries + 1):
# Check for interruption
if self._is_interrupted:
break
# Filter out URLs that have reached max attempts
to_retry = [idx for idx in failed
if self.attempt_counts[idx] < self.args.retries + 1]
if not to_retry:
break
console.print(
f"[dim]Retry round {retry_num}/{self.args.retries} for {len(to_retry)} URLs[/dim]")
retry_results = await self.run_phase(f"Retry {retry_num}", to_retry, semaphore)
# Check for interruption
if self._is_interrupted:
break
# Update results
for r in retry_results:
if r.index - 1 < len(self.results):
self.results[r.index - 1] = r
# Update failed list for next round
failed = [r.index for r in self.results if r.status == DownloadStatus.FAILED]
# Exponential backoff between retry rounds
if retry_num < self.args.retries and failed and not self._is_interrupted:
backoff = min(60, 2 ** retry_num + random.uniform(0, 2))
console.print(f"[dim]Waiting {backoff:.1f}s before next retry round...[/dim]")
# Wait with shutdown check
try:
await asyncio.wait_for(self._shutdown_event.wait(), timeout=backoff)
if self._is_interrupted:
break
except asyncio.TimeoutError:
pass
# Print summary if not interrupted
if not self._is_interrupted:
self.print_summary()
else:
console.print("
[yellow]⚠️  Download interrupted by user[/yellow]")
return 0
except GracefulExit:
# Already handled interruption
return 130
finally:
# Always clean up partial files
self.cleanup_partial_files()
def parse_args():
parser = argparse.ArgumentParser(
description="v2down — by Mmdre",
formatter_class=argparse.RawDescriptionHelpFormatter,
epilog="""
Examples:
%(prog)s
%(prog)s --input feeds.txt --workers 4 --output-dir downloads/
%(prog)s --skip-existing --respect-robots --log-file v2down.log
"""
)
parser.add_argument("--version", action="version", version=f"v2down {__version__}")
parser.add_argument(
"--input", "-i",
type=str,
default="subscriptions.txt",
help="URL list file (default: subscriptions.txt)"
)
parser.add_argument(
"--output-dir", "-o",
type=str,
default="raw-v2ray",
help="Where to save files (default: raw-v2ray)"
)
parser.add_argument(
"--skip-existing",
action="store_true",
help="Skip if final file exists"
)
parser.add_argument(
"--workers", "-w",
type=int,
default=1,
help="Parallel downloads (default: 1; 0 = auto based on CPU)"
)
parser.add_argument(
"--max-visible",
type=int,
default=8,
help="Maximum URLs to show in display (default: 8)"
)
parser.add_argument(
"--timeout",
type=float,
default=20.0,
help="Request timeout in seconds (default: 20.0)"
)
parser.add_argument(
"--retries",
type=int,
default=4,
help="Extra retry attempts (default: 4 → total 5)"
)
parser.add_argument(
"--delay-min",
type=float,
default=0.6,
help="Min random delay between requests (default: 0.6s)"
)
parser.add_argument(
"--delay-max",
type=float,
default=2.0,
help="Max random delay between requests (default: 2.0s)"
)
parser.add_argument(
"--respect-robots",
action="store_true",
help="Check robots.txt before fetching"
)
parser.add_argument(
"--header", "-H",
action="append",
default=[],
help='Add custom header (repeatable, e.g., "User-Agent: MyBot/1.0")'
)
parser.add_argument(
"--log-file",
type=str,
help="Append detailed log to this file"
)
parser.add_argument(
"--verbose", "-v",
action="count",
default=0,
help="Increase verbosity (use -vv for debug)"
)
return parser.parse_args()
console = Console()
def main():
args = parse_args()
# Validate workers
if args.workers < 0:
console.print("[red]Error: --workers must be >= 0[/red]")
return 1
if args.workers == 0:
args.workers = min(16, (os.cpu_count() or 4) * 2)
# Validate retries
if args.retries < 0:
console.print("[red]Error: --retries must be >= 0[/red]")
return 1
# Validate max_visible
if args.max_visible < 3:
args.max_visible = 3
elif args.max_visible > 20:
args.max_visible = 20
# Run the downloader
downloader = Downloader(args)
try:
return asyncio.run(downloader.run())
except KeyboardInterrupt:
# This handles the second Ctrl+C (force quit)
console.print("
[red]Force quit[/red]")
# Still try to clean up partial files on force quit
downloader.cleanup_partial_files()
return 130
except GracefulExit:
return 130
except Exception as e:
console.print(f"[red]Unexpected error: {e}[/red]", style="bold")
import traceback
if args.verbose >= 2:
console.print(traceback.format_exc())
return 1
if __name__ == "__main__":
sys.exit(main())
