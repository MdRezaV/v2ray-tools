#!/usr/bin/env python3
"""v2conv — Telegram Proxy → v2ray Config Converter

Convert Telegram SOCKS5/MTProto proxy links into v2ray-compatible URLs.
Native v2ray schemes (vmess/vless/trojan/ss/etc.) pass through unchanged.
Private/local IPs are filtered by default; use --allow-local to override.

Usage:
  v2conv "https://t.me/socks?server=proxy.example.com&port=1080"
  v2conv -c                      # read from clipboard
  v2conv -w -o configs.txt       # watch clipboard, save results
  v2conv proxies.txt -o out.txt  # batch convert from file
  v2conv -c -q | grep -v '^#'    # quiet mode for scripting

Options:
  INPUT:  URL/file | -c/--clipboard | -w/--watch
  OUTPUT: -o FILE | -a/--append | -q/--quiet
  FLAGS:  -d/--debug | --allow-local | -s SELECTION
  WATCH:  --watch-interval SECONDS (default: 1.0)

Supported: Telegram SOCKS5/MTProto | Native v2ray: vmess/vless/trojan/ss/ssr/socks/mtproto/hysteria/tuic

Dependencies: Standard library + clipboard tools (xclip/xsel/wl-clipboard on Linux, pyperclip cross-platform)
Exit codes: 0 = success | 1 = error
"""

import argparse, base64, ipaddress, os, re, subprocess, sys, time, urllib.parse, signal
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional


_watch_shutdown = False


def _handle_shutdown(signum, frame):
    global _watch_shutdown
    _watch_shutdown = True
    print(f"\n{Colors.info('ℹ️')} Stopping clipboard watcher...", file=sys.stderr)


class Config:
    V2RAY_SCHEMES = frozenset(['vmess','vless','trojan','ss','ssr','socks','mtproto','hysteria','tuic'])
    TELEGRAM_DOMAINS = frozenset(['t.me','socks','proxy'])
    PRIVATE_PATTERNS = [
        r'^127(\.\d{1,3}){3}$', r'^10(\.\d{1,3}){3}$',
        r'^172\.(1[6-9]|2[0-9]|3[0-1])(\.\d{1,3}){2}$', r'^192\.168(\.\d{1,3}){2}$',
        r'^169\.254(\.\d{1,3}){2}$', r'^0\.0\.0\.0$', r'^::1$', r'^fe80:', r'^fc00:',
    ]
    LOCALHOST_NAMES = frozenset(['localhost','localhost.localdomain'])
    URL_PATTERNS = [
        r'https?://t\.me/(?:socks|proxy)\?[^\s"\'<>]+',
        r'tg://(?:socks|proxy)\?[^\s"\'<>]+',
        r'(?:vmess|vless|trojan|ss|ssr|socks|mtproto|hysteria|tuic)://[^\s]+',
    ]
    TRUNCATE_LEN, TYPE_WIDTH = 45, 8


class Colors:
    _enabled = sys.stdout.isatty()
    RED, GREEN, YELLOW, BLUE, CYAN, BOLD, RESET = '\033[31m','\033[32m','\033[33m','\033[34m','\033[36m','\033[1m','\033[0m'
    TYPE_COLORS = {'socks':BLUE,'mtproto':CYAN,'passthrough':GREEN,'v2ray':GREEN}
    EMOJI = {'success':'🟩','error':'🟥','warn':'⚠️','info':'ℹ️','pass':'🟧','skip':'⬜','clip':'📋','file':'📁','watch':'👁️'}

    @classmethod
    def _wrap(cls, t:str, c:str) -> str: return f"{c}{t}{cls.RESET}" if cls._enabled else t
    @classmethod
    def ok(cls,t:str)->str: return cls._wrap(t,cls.GREEN)
    @classmethod
    def err(cls,t:str)->str: return cls._wrap(t,cls.RED)
    @classmethod
    def warn(cls,t:str)->str: return cls._wrap(t,cls.YELLOW)
    @classmethod
    def info(cls,t:str)->str: return cls._wrap(t,cls.CYAN)
    @classmethod
    def bold(cls,t:str)->str: return cls._wrap(t,cls.BOLD)
    @classmethod
    def for_type(cls,pt:str)->str:
        return cls._wrap(f"{pt.upper():{Config.TYPE_WIDTH}}",cls.TYPE_COLORS.get(pt.lower(),cls.YELLOW))
    @classmethod
    def emoji(cls,k:str)->str: return cls.EMOJI.get(k,'')


class ProxyType(Enum):
    SOCKS,MTPROTO,V2RAY,UNKNOWN = auto(),auto(),auto(),auto()


@dataclass(frozen=True)
class ProxyConfig:
    scheme:str; host:str; port:str; user:Optional[str]=None; password:Optional[str]=None
    secret:Optional[str]=None; label:Optional[str]=None

    def to_v2ray_url(self)->str:
        if self.scheme=='socks':
            creds=f"{self.user or ''}:{self.password or ''}"
            auth=base64.b64encode(creds.encode()).decode().rstrip('=')
            return f"socks://{auth}@{self.host}:{self.port}#{self.host}"
        if self.scheme=='mtproto':
            secret=(self.secret or'').strip()
            if len(secret)<32: raise ValueError("MTProto secret must be ≥32 characters")
            return f"mtproto://{secret}@{self.host}:{self.port}#{self.host}"
        raise ValueError(f"Cannot convert scheme '{self.scheme}'")


@dataclass
class ConversionResult:
    proxy_type:ProxyType; original:str; result:str; skipped:bool=False; error:Optional[str]=None
    @property
    def success(self)->bool: return not self.skipped and not self.error


def extract_host(url:str)->Optional[str]:
    url=url.strip(); parsed=urllib.parse.urlparse(url)
    if parsed.query and _is_telegram_url(parsed):
        params=urllib.parse.parse_qs(parsed.query)
        if srv:=params.get('server',[None])[0]: return srv.strip()
    if parsed.hostname: return parsed.hostname
    return _extract_host_fallback(url)

def _is_telegram_url(p:urllib.parse.ParseResult)->bool:
    return any(d in p.netloc.lower() for d in Config.TELEGRAM_DOMAINS)

def _extract_host_fallback(url:str)->Optional[str]:
    if m:=re.search(r'\[([^\]]+)\]',url): return m.group(1)
    if m:=re.search(r'://(?:[^@/]+@)?([^:/?#\[\]+]+)',url): return m.group(1)
    return None

def is_private_ip(host:Optional[str])->bool:
    if not host: return True
    if host.lower() in Config.LOCALHOST_NAMES: return True
    clean=host[1:-1] if host.startswith('[') and host.endswith(']') else host
    try:
        ip=ipaddress.ip_address(clean)
        return any([ip.is_private,ip.is_loopback,ip.is_link_local,ip.is_unspecified,ip.is_multicast])
    except ValueError:
        return any(re.match(pat,clean,re.I) for pat in Config.PRIVATE_PATTERNS)

def detect_proxy_type(url:str)->ProxyType:
    ul=url.lower().strip()
    if any(ul.startswith(f"{s}://") for s in Config.V2RAY_SCHEMES): return ProxyType.V2RAY
    if '/socks?' in ul or ul.startswith('tg://socks?'): return ProxyType.SOCKS
    if '/proxy?' in ul or 'secret=' in ul or ul.startswith('tg://proxy?'): return ProxyType.MTPROTO
    return ProxyType.UNKNOWN

def parse_telegram_socks(url:str)->ProxyConfig:
    parsed=urllib.parse.urlparse(url.strip())
    if not(_is_telegram_url(parsed) and parsed.netloc.lower() in['socks','t.me']):
        raise ValueError("Invalid Telegram SOCKS URL format")
    params=urllib.parse.parse_qs(parsed.query)
    srv,port=params.get('server',[None])[0],params.get('port',[None])[0]
    if not srv or not port: raise ValueError("SOCKS proxy requires 'server' and 'port'")
    return ProxyConfig('socks',srv.strip(),port.strip(),
        params.get('user',[''])[0]or None,params.get('pass',[''])[0]or None)

def parse_telegram_mtproto(url:str)->ProxyConfig:
    parsed=urllib.parse.urlparse(url.strip())
    if not(_is_telegram_url(parsed) and parsed.netloc.lower() in['proxy','t.me']):
        raise ValueError("Invalid MTProto URL format")
    params=urllib.parse.parse_qs(parsed.query)
    srv,port,sec=params.get('server',[None])[0],params.get('port',[None])[0],params.get('secret',[None])[0]
    if not all([srv,port,sec]): raise ValueError("MTProto requires 'server', 'port', 'secret'")
    return ProxyConfig('mtproto',srv.strip(),port.strip(),sec.strip())

def convert_proxy(url:str,allow_local:bool=False)->ConversionResult:
    ptype=detect_proxy_type(url)
    if not allow_local:
        host=extract_host(url)
        if host and is_private_ip(host):
            return ConversionResult(ptype,url,f"SKIPPED: Private IP ({host})",skipped=True)
    try:
        if ptype==ProxyType.V2RAY: return ConversionResult(ptype,url,url)
        if ptype==ProxyType.SOCKS:
            return ConversionResult(ptype,url,parse_telegram_socks(url).to_v2ray_url())
        if ptype==ProxyType.MTPROTO:
            return ConversionResult(ptype,url,parse_telegram_mtproto(url).to_v2ray_url())
        raise ValueError("Unknown proxy format — supported: Telegram SOCKS/MTProto or native v2ray schemes")
    except Exception as e:
        return ConversionResult(ptype,url,f"ERROR: {e}",error=str(e))

def extract_urls(text:str,debug:bool=False)->list[str]:
    urls=[]
    for pat in Config.URL_PATTERNS:
        for m in re.findall(pat,text,re.I):
            cleaned=re.sub(r'[^\w@:.?=&/%#\-_~]+$', '', m)
            if cleaned: urls.append(cleaned)
    if debug and urls: _debug_extracted_urls(urls)
    return urls

def _truncate(t:str,max_len:int=Config.TRUNCATE_LEN)->str:
    return t.ljust(max_len) if len(t)<=max_len else t[:max_len-1]+'…'

def _debug_extracted_urls(urls:list[str])->None:
    print(Colors.info(f"{Colors.emoji('info')} Found {len(urls)} URL(s)"))
    for i,u in enumerate(urls,1):
        host=extract_host(u); local=f" {Colors.warn('[local]')}" if host and is_private_ip(host) else""
        print(f"  {i}. {Colors.for_type(detect_proxy_type(u).name)} {_truncate(u)}{local}")

def _run_command(cmd:list[str],timeout:int=5)->Optional[str]:
    try:
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout,check=False)
        return r.stdout if r.returncode==0 else None
    except(subprocess.TimeoutExpired,OSError): return None

def _has_pyperclip()->bool:
    try: import pyperclip; return True
    except ImportError: return False

def _read_linux_clipboard(selection:str,debug:bool)->Optional[str]:
    x11={'primary':'-selection primary','secondary':'-selection secondary','clipboard':'-selection clipboard'}
    flag=x11.get(selection,'-selection clipboard')
    for cmd in[['xclip',*flag.split(),'-o'],['xsel',{'primary':'--primary','secondary':'--secondary'}.get(selection,'--clipboard'),'--output']]:
        if c:=_run_command(cmd):
            if c.strip():
                if debug: print(Colors.ok(f"{Colors.emoji('success')} {cmd[0]}"))
                return c
    if selection=='clipboard' and _run_command(['wl-paste','--help']):
        if c:=_run_command(['wl-paste']):
            if c.strip():
                if debug: print(Colors.ok(f"{Colors.emoji('success')} wl-paste"))
                return c
    return None

def read_clipboard(selection:str='clipboard',debug:bool=False)->str:
    is_linux=sys.platform.startswith('linux')
    has_disp=bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))
    if debug:
        print(Colors.info(f"{Colors.emoji('clip')} Clipboard: {sys.platform}, pyperclip={'✓'if _has_pyperclip()else'✗'}"))
    if is_linux and has_disp:
        if c:=_read_linux_clipboard(selection,debug): return c
    if _has_pyperclip():
        try:
            import pyperclip
            if c:=pyperclip.paste():
                if c.strip():
                    if debug: print(Colors.ok(f"{Colors.emoji('success')} pyperclip"))
                    return c
        except Exception as e:
            if debug: print(Colors.warn(f"{Colors.emoji('error')} pyperclip: {e}"))
    _raise_clipboard_error(is_linux)

def _raise_clipboard_error(is_linux:bool)->None:
    if is_linux:
        st=os.environ.get('XDG_SESSION_TYPE','x11')
        pkg='wl-clipboard' if st=='wayland' else'xclip'
        raise RuntimeError(f"Cannot access clipboard. Install:\n  • Wayland: sudo pacman -S wl-clipboard\n  • X11: sudo pacman -S xclip\n  • Or: pip install pyperclip")
    raise RuntimeError("Cannot access clipboard. Install: pip install pyperclip")

def _is_direct_url(arg:str)->bool:
    pats=[r'(?:https?://t\.me|tg://)(?:socks|proxy)\?',r'(?:vmess|vless|trojan|ss|ssr|socks|mtproto)://']
    return any(re.match(p,arg,re.I) for p in pats) or arg.startswith(('http://','https://','tg://'))

def load_input_lines(inp:Optional[str],from_clip:bool,selection:str,debug:bool)->list[str]:
    if from_clip: return read_clipboard(selection,debug).splitlines()
    if not inp: return[]
    if _is_direct_url(inp): return[inp]
    if Path(inp).is_file():
        with open(inp,encoding='utf-8') as f: return f.readlines()
    raise ValueError(f"Invalid input: '{inp}' is neither URL nor readable file")

@dataclass
class ConversionStats:
    converted:int=0; passthrough:int=0; failed:int=0; skipped:int=0
    @property
    def total_success(self)->int: return self.converted+self.passthrough
    def summary(self,out:Optional[str],append:bool,quiet:bool)->str:
        if quiet: return""
        if out:
            em=Colors.emoji('file'); mode="Appended to" if append else"Saved to"
            print(); print(Colors.info(f"{em} {mode} {out}"))
        parts=[]
        if self.converted: parts.append(Colors.ok(f"{Colors.emoji('success')} {self.converted} converted"))
        if self.passthrough: parts.append(Colors.info(f"{Colors.emoji('pass')} {self.passthrough} passed through"))
        if self.skipped: parts.append(Colors.warn(f"{Colors.emoji('skip')} {self.skipped} skipped (private IP)"))
        if self.failed: parts.append(Colors.err(f"{Colors.emoji('error')} {self.failed} failed"))
        return Colors.bold(" | ".join(parts)) if parts else""

def process_urls(urls:list[str],allow_local:bool,debug:bool,quiet:bool)->tuple[list[str],ConversionStats]:
    results,stats=[],ConversionStats()
    for line in urls:
        line=line.strip()
        if not line or line.startswith('#'): continue
        found=extract_urls(line,debug)
        if not found and detect_proxy_type(line)!=ProxyType.UNKNOWN: found=[line]
        for url in found: _handle_result(convert_proxy(url,allow_local),debug,quiet,stats,results)
    return results,stats

def _handle_result(res:ConversionResult,debug:bool,quiet:bool,stats:ConversionStats,results:list[str])->None:
    ptype=Colors.for_type(res.proxy_type.name); orig=_truncate(res.original)
    if res.skipped:
        stats.skipped+=1
        if debug or not quiet: print(f"{Colors.warn(Colors.emoji('skip'))} {ptype} {orig} → {Colors.warn('[filtered]')}")
    elif res.error:
        stats.failed+=1
        if not quiet: print(f"{Colors.err(Colors.emoji('error'))} {ptype} {orig} → {Colors.err(res.error)}")
    else:
        results.append(res.result)
        if res.proxy_type==ProxyType.V2RAY:
            stats.passthrough+=1; sym=Colors.info(Colors.emoji('pass')); out=Colors.ok(_truncate(res.result))
        else:
            stats.converted+=1; sym=Colors.ok(Colors.emoji('success')); out=Colors.ok(_truncate(res.result))
        if not quiet: print(f"{sym} {ptype} {orig} → {out}")

def write_results(results:list[str],path:Optional[str],append:bool)->None:
    if not path: return
    with open(path,'a'if append else'w',encoding='utf-8') as f:
        for u in results: f.write(u+'\n')

def parse_arguments()->argparse.Namespace:
    epilog="""
Examples:
  Convert a Telegram SOCKS5 proxy:
    v2conv "https://t.me/socks?server=proxy.example.com&port=1080&user=alice&pass=secret123"

  Batch convert from clipboard, save to file:
    v2conv -c -o v2ray_configs.txt

  Watch clipboard continuously:
    v2conv -w
    v2conv -w -o collected_configs.txt -a

  Quiet mode for scripting:
    v2conv -w -q | tee configs.txt

  Debug extraction issues:
    v2conv -c -d

  Custom watch interval:
    v2conv -w --watch-interval 5

  Allow private/local IPs:
    v2conv -c --allow-local
"""
    parser=argparse.ArgumentParser(prog='v2conv',description='Convert Telegram proxy links to v2ray configuration format',
        epilog=epilog,formatter_class=argparse.RawDescriptionHelpFormatter,add_help=True)
    parser.add_argument('input',nargs='?',metavar='INPUT',help='Proxy URL or input file path')
    parser.add_argument('-c','--clipboard',action='store_true',help='Read from system clipboard')
    parser.add_argument('-w','--watch',action='store_true',help='Watch clipboard continuously (Ctrl+C to stop)')
    parser.add_argument('--watch-interval',type=float,default=1.0,metavar='SECONDS',help='Watch mode poll interval [default: 1.0]')
    parser.add_argument('-o','--output',metavar='FILE',help='Write results to FILE')
    parser.add_argument('-a','--append',action='store_true',help='Append to output file')
    parser.add_argument('-q','--quiet',action='store_true',help='Output only converted URLs')
    parser.add_argument('-d','--debug',action='store_true',help='Verbose debug output')
    parser.add_argument('--allow-local',action='store_true',help='Allow private/local IP proxies')
    parser.add_argument('-s','--selection',choices=['clipboard','primary','secondary'],default='clipboard',
        help='Linux/X11 clipboard selection [default: clipboard]')
    return parser.parse_args()

def watch_clipboard(allow_local:bool,debug:bool,quiet:bool,output:Optional[str],append:bool,
                   interval:float,selection:str)->int:
    global _watch_shutdown
    _watch_shutdown = False
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    last_content = ""
    total_stats = ConversionStats()

    if not quiet:
        print(Colors.info(f"{Colors.emoji('watch')} Watching clipboard... (interval: {interval}s, Ctrl+C to stop)"))

    try:
        while not _watch_shutdown:
            try:
                current_content = read_clipboard(selection, debug=False)
                if current_content != last_content:
                    last_content = current_content
                    lines = current_content.splitlines()
                    if debug:
                        print(Colors.info(f"{Colors.emoji('clip')} Clipboard changed, processing {len(lines)} line(s)..."))
                    results, stats = process_urls(lines, allow_local, debug, quiet)
                    total_stats.converted += stats.converted
                    total_stats.passthrough += stats.passthrough
                    total_stats.failed += stats.failed
                    total_stats.skipped += stats.skipped
                    if output and results:
                        write_results(results, output, append=True)
                    if not quiet and (stats.converted or stats.passthrough or stats.failed or stats.skipped):
                        print(Colors.info(f"  └─ Batch: {stats.converted} converted, {stats.passthrough} passed, {stats.skipped} skipped, {stats.failed} failed"))
                for _ in range(int(interval * 10)):
                    if _watch_shutdown: break
                    time.sleep(0.1)
            except Exception as e:
                if debug:
                    print(Colors.err(f"{Colors.emoji('error')} Watch loop error: {e}"), file=sys.stderr)
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        if not quiet:
            print()
            if summary := total_stats.summary(output, True, quiet):
                print(f"{Colors.bold('Watch session ended.')} {summary}")
            else:
                print(Colors.info(f"{Colors.emoji('info')} No proxies processed."))
    return 0 if total_stats.total_success > 0 else 1

def main()->int:
    args=parse_arguments()
    if args.watch and (args.input or args.clipboard):
        print("Error: --watch cannot be combined with input file or --clipboard", file=sys.stderr)
        return 1
    if not args.input and not args.clipboard and not args.watch:
        print("Error: Provide URL, file, -c for clipboard, or -w to watch. Use -h for examples",file=sys.stderr)
        return 1
    if (args.clipboard or args.watch) and not _has_pyperclip():
        tools=['xclip','xsel','wl-paste']
        if not any(_run_command([t,'-h']) or _run_command([t,'--help']) for t in tools):
            print(f"{Colors.err(Colors.emoji('error'))} Clipboard tools not found.\nInstall: xclip/xsel (X11), wl-clipboard (Wayland), or pip install pyperclip",file=sys.stderr)
            return 1
    if args.watch:
        return watch_clipboard(
            allow_local=args.allow_local, debug=args.debug, quiet=args.quiet,
            output=args.output, append=args.append, interval=args.watch_interval, selection=args.selection
        )
    try:
        lines=load_input_lines(args.input,args.clipboard,args.selection,args.debug)
        results,stats=process_urls(lines,args.allow_local,args.debug,args.quiet)
        write_results(results,args.output,args.append)
        if summary:=stats.summary(args.output,args.append,args.quiet): print(f"\n{summary}")
        if stats.total_success==0:
            msg="No valid proxies found"+(f" ({stats.skipped} filtered)" if stats.skipped else"")
            print(Colors.err(f"{Colors.emoji('error')} {msg}"),file=sys.stderr)
            return 1
        return 0
    except Exception as e:
        print(Colors.err(f"{Colors.emoji('error')} Fatal error: {e}"),file=sys.stderr)
        return 1

if __name__=="__main__": sys.exit(main())
