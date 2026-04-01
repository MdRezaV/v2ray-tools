# V2Ray Tools Collection

A set of command-line tools for managing V2Ray configurations – downloading subscriptions, sorting by country, filtering by address patterns, mapping IP addresses to countries, and converting Telegram proxy links to v2ray-compatible URLs.

## Tools

| Script | Purpose |
|--------|---------|
| **v2down** | Download V2Ray subscription URLs (or any text‑based subscription) with resume, concurrency, and rich progress display. |
| **v2cidr** | Organize V2Ray configurations by country using MaxMind GeoLite2 database or custom CIDR range files. |
| **v2find** | Filter V2Ray configurations by server address patterns (wildcards, IP ranges, domains) with optional DNS resolution. |
| **ip2cc** | Map IP addresses to countries using CIDR range files. |
| **v2conv** | Convert Telegram SOCKS5/MTProto proxy links into v2ray-compatible URLs. Native v2ray schemes pass through unchanged. |

---

## Installation

### Prerequisites
- Python 3.7 or higher
- pip

### Install dependencies

```bash
pip install httpx rich tenacity python-v2ray pycountry maxminddb pyperclip
```

For `ip2cc`, the dependencies are `pycountry` and `rich` (both optional, but recommended).

For `v2conv` clipboard functionality on Linux:
- **X11**: Install `xclip` or `xsel` (`sudo pacman -S xclip` or `sudo apt install xclip`)
- **Wayland**: Install `wl-clipboard` (`sudo pacman -S wl-clipboard` or `sudo apt install wl-clipboard`)
- **Cross-platform fallback**: `pyperclip` (installed above) handles clipboard access when native tools aren't available.

*Note: `maxminddb` is required only for `v2cidr` when using the MMDB mode.  
`python-v2ray` is required for `v2cidr` and `v2find`.*

---

## Usage

### 1. `v2down` – Download V2Ray subscriptions

Downloads each URL from the input file to a numbered `.txt` file. Supports parallel downloads, partial resume, and robots.txt.

```bash
v2down --input urls.txt --workers 4 --output-dir downloads/
```

**Options**

| Option | Description |
|--------|-------------|
| `-i, --input` | Input file with one URL per line (default: `subscriptions.txt`) |
| `-o, --output-dir` | Output directory (default: `raw-v2ray`) |
| `-w, --workers` | Number of parallel downloads (default: 1) |
| `--skip-existing` | Skip if final file already exists |
| `--respect-robots` | Check `robots.txt` before fetching |
| `--retries` | Number of retry attempts (default: 4) |
| `--delay-min, --delay-max` | Random delay between requests (default: 0.6–2.0 seconds) |
| `--header` | Add custom headers (repeatable) |
| `--log-file` | Append detailed log to file |

**Example**  
Download from multiple subscriptions with 8 workers, respecting robots.txt, and skipping existing files:

```bash
v2down -i subs.txt -w 8 --skip-existing --respect-robots
```

---

### 2. `v2cidr` – Organize configurations by country

Reads V2Ray URIs from text files, resolves domains, and writes each configuration to a file named `{COUNTRY_CODE}.{COUNTRY_NAME}.txt`.  
Two geolocation methods are available: MaxMind GeoLite2 database (default) or CIDR range files.

```bash
v2cidr configs/*.txt --use-mmdb --mmdb-file GeoLite2-Country.mmdb -w 8
```

**Options**

| Option | Description |
|--------|-------------|
| `files` | Input files (supports glob patterns like `*.txt`) |
| `--use-mmdb` | Use MaxMind MMDB database (default) |
| `--use-cidr` | Use CIDR range files |
| `--mmdb-file` | Path to GeoLite2‑Country.mmdb (default: `GeoLite2-Country.mmdb`) |
| `--cidr-dir` | Directory containing CIDR files (default: `cidr`) |
| `-w, --workers` | Number of worker threads (default: 4) |
| `-s, --skip-duplicates` | Remove duplicate configurations (based on server:port) |
| `-v, --verbose` | Show more details |
| `--output-dir` | Output directory (default: `by-country`) |

**CIDR file format**  
Place CIDR files in the `--cidr-dir` folder. Naming convention:
- IPv4: `{CC}.txt` (e.g., `US.txt`)
- IPv6: `{CC}.ipv6.txt` (e.g., `US.ipv6.txt`)

Each file contains one CIDR range per line (comments and empty lines are ignored).  
You can obtain CIDR ranges from [cidr-ip-ranges-by-country](https://github.com/ipverse/cidr-ip-ranges-by-country).

**MMDB database**  
The script expects a GeoLite2 Country database in MaxMind DB format. You can download it from [GeoLite.mmdb](https://github.com/P3TERX/GeoLite.mmdb) or from [MaxMind's official site](https://dev.maxmind.com/geoip/geolite2-free-geolocation-data).

**Example**  
Process all `.txt` files, use CIDR ranges, skip duplicates, with 8 threads:

```bash
v2cidr *.txt --use-cidr --cidr-dir cidr/ -s -w 8
```

---

### 3. `v2find` – Filter configurations by address pattern

Searches for V2Ray configurations whose server address matches a given pattern (wildcards allowed). Optionally resolves domains to IPs to match against IP patterns.

```bash
v2find *.txt -addr "127.0.*" -o found.txt
```

**Options**

| Option | Description |
|--------|-------------|
| `files` | Input files (glob patterns supported) |
| `-addr, --address` | Address pattern (wildcard `*` allowed) – **required** |
| `-o, --output` | Output file (default: print to stdout) |
| `-w, --workers` | Number of worker threads (default: 4) |
| `-s, --skip-duplicates` | Remove duplicate configurations |
| `--no-resolve` | Disable DNS resolution (only match the domain string) |

**Pattern examples**
- `127.0.*` – matches addresses starting with `127.0.`
- `*.185` – matches addresses ending with `.185`
- `192.168.*.*` – matches any IP in the 192.168.x.x range
- `example.*` – matches domains starting with `example.`
- `*.example.com` – matches subdomains of `example.com`

**Example**  
Find all configurations using Cloudflare IPs and save to file:

```bash
v2find configs/*.txt -addr "104.16.*" -o cloudflare.txt
```

---

### 4. `ip2cc` – Map IP addresses to countries

Maps IP addresses to country codes using CIDR range files. Supports batch lookups, various output formats, reverse lookups, and exporting CIDR ranges.

```bash
ip2cc 8.8.8.8
```

**Options**

| Option | Description |
|--------|-------------|
| `ips_or_command` | IP addresses to lookup or `stats` for database statistics |
| `-v, --verbose` | Enable verbose output |
| `--input-file` | Read IPs from a file (one per line) |
| `--output-format` | Output format: `text`, `json`, `csv`, `table` (default: `text`) |
| `--reverse` | Show sample IPs for a given country code |
| `--export` | Export all CIDR ranges for a country to stdout |
| `--limit` | Limit for reverse lookup (default: 10) |
| `--cidr-folder` | Path to CIDR folder (default: `CIDR`) |

**CIDR folder structure**  
Place CIDR files in the `--cidr-folder` directory. Files should be named like:
- `US-ipv4-Hackers.Zone.txt`
- `US-ipv6-Hackers.Zone.txt`

Each file contains one CIDR range per line. Comments (lines starting with `#`) and empty lines are ignored. You can obtain CIDR ranges from [cidr-ip-ranges-by-country](https://github.com/ipverse/cidr-ip-ranges-by-country).

**Examples**

Lookup a single IP:
```bash
ip2cc 8.8.8.8
```

Lookup multiple IPs:
```bash
ip2cc 8.8.8.8 1.1.1.1 2001:4860:4860::8888
```

Read IPs from a file and output JSON:
```bash
ip2cc --input-file ips.txt --output-format json
```

Show database statistics:
```bash
ip2cc stats
```

Show sample IPs for United States:
```bash
ip2cc --reverse US --limit 5
```

Export all CIDR ranges for Japan:
```bash
ip2cc --export JP > jp_cidrs.txt
```

---

### 5. `v2conv` – Telegram Proxy → v2ray Config Converter

Convert Telegram SOCKS5/MTProto proxy links into v2ray-compatible URLs. Native v2ray schemes (`vmess`/`vless`/`trojan`/`ss`/`ssr`/`socks`/`mtproto`/`hysteria`/`tuic`) pass through unchanged. Private/local IPs are filtered by default; use `--allow-local` to override.

**Supported Input Formats:**
- Telegram SOCKS5: `https://t.me/socks?server=proxy.example.com&port=1080&user=alice&pass=secret`
- Telegram MTProto: `https://t.me/proxy?server=proxy.example.com&port=443&secret=xxxx`
- Native v2ray URLs: `vmess://`, `vless://`, `trojan://`, `ss://`, `ssr://`, `socks://`, `mtproto://`, `hysteria://`, `tuic://`

**Basic Usage:**
```bash
# Convert a single Telegram SOCKS5 proxy URL
v2conv "https://t.me/socks?server=proxy.example.com&port=1080"

# Read from clipboard
v2conv -c

# Watch clipboard continuously (Ctrl+C to stop)
v2conv -w -o configs.txt

# Batch convert from file
v2conv proxies.txt -o out.txt

# Quiet mode for scripting (output only converted URLs)
v2conv -c -q | grep -v '^#'
```

**Options**

| Option | Description |
|--------|-------------|
| `INPUT` | Proxy URL, input file path, `-c` for clipboard, or `-w` to watch clipboard |
| `-c, --clipboard` | Read from system clipboard |
| `-w, --watch` | Watch clipboard continuously (Ctrl+C to stop) |
| `--watch-interval SECONDS` | Watch mode poll interval (default: `1.0`) |
| `-o, --output FILE` | Write results to FILE |
| `-a, --append` | Append to output file instead of overwriting |
| `-q, --quiet` | Output only converted URLs (suppress status messages) |
| `-d, --debug` | Verbose debug output |
| `--allow-local` | Allow private/local IP proxies (disabled by default for security) |
| `-s, --selection` | Linux/X11 clipboard selection: `clipboard`, `primary`, or `secondary` (default: `clipboard`) |

**Output Format:**
- **SOCKS5**: `socks://base64(user:pass)@host:port#host`
- **MTProto**: `mtproto://secret@host:port#host`
- **Native v2ray**: Passed through unchanged

**Examples**

Convert a Telegram SOCKS5 proxy with credentials:
```bash
v2conv "https://t.me/socks?server=proxy.example.com&port=1080&user=alice&pass=secret123"
# Output: socks://YWxpY2U6c2VjcmV0MTIz@proxy.example.com:1080#proxy.example.com
```

Batch convert from clipboard, save to file:
```bash
v2conv -c -o v2ray_configs.txt
```

Watch clipboard continuously and append results:
```bash
v2conv -w -o collected_configs.txt -a
```

Allow private/local IPs (use with caution):
```bash
v2conv -c --allow-local
```

**Exit Codes:**
- `0` = Success (at least one proxy converted or passed through)
- `1` = Error (no valid proxies found, or fatal error)

---

## Example Workflow

1. **Download** subscriptions:
   ```bash
   v2down -i my-subs.txt -w 8 -o raw/
   ```

2. **Convert** any Telegram proxy links found:
   ```bash
   v2conv raw/*.txt -o converted.txt
   ```

3. **Filter** by address (e.g., keep only servers with IPs in US range):
   ```bash
   v2find converted.txt -addr "192.168.*" -o filtered.txt
   ```

4. **Sort by country** using CIDR files:
   ```bash
   v2cidr filtered.txt --use-cidr --cidr-dir cidr/ -s -w 8
   ```

Now you have country‑organized, v2ray-compatible configurations ready to use.

---

## License

This project is licensed under the MIT License – see the [LICENSE](LICENSE) file for details.

---

## Contributing

Contributions are welcome! Please open an issue or pull request.

---

## Acknowledgements

- [python-v2ray](https://github.com/Qv2ray/python-v2ray) for V2Ray configuration parsing
- [Rich](https://github.com/Textualize/rich) for terminal output formatting
- [httpx](https://github.com/encode/httpx) for asynchronous HTTP requests
- [MaxMind](https://www.maxmind.com) for GeoLite2 data
- [pycountry](https://github.com/flyingcircusio/pycountry) for country names
- [cidr-ip-ranges-by-country](https://github.com/ipverse/cidr-ip-ranges-by-country) for CIDR data
- [GeoLite.mmdb](https://github.com/P3TERX/GeoLite.mmdb) for MMDB database
- [pyperclip](https://github.com/asweigart/pyperclip), [xclip](https://github.com/astrand/xclip), [wl-clipboard](https://github.com/bugaevc/wl-clipboard) for clipboard access
