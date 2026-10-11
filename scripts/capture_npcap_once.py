#!/usr/bin/env python3
"""Bounded passive AION2 socket capture. No UI, routes, replay or resident process.

Original bytes are written unopened to the existing private spool. Only file
digest/size and capture-header counters leave this controller-side recorder.
"""
import argparse
import ctypes as ct
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time
import uuid

PRIVATE_ROOT = Path(os.environ.get(
    'AION2_CAPTURE_SPOOL',
    'C:/Users/user/Documents/Codex/2026-09-28/new-chat/work/capture-ready',
))
MAX_FILE = 64 * 1024 * 1024
SAFE_ERROR_CODES = {
    'active_capture_or_stale_stop_present',
    'capture_device_query_failed',
    'capture_device_scope_invalid',
    'game_not_running_or_loopback_sockets_missing',
    'game_scope_query_failed',
    'game_socket_scope_too_large',
    'invalid_socket_port',
    'npcap_address_family_mismatch',
    'npcap_capture_header_invalid',
    'npcap_dynamic_filter_update_failed',
    'npcap_ip_version_invalid',
    'npcap_pseudo_header_invalid',
    'npcap_read_failed',
    'npcap_scoped_filter_setup_failed',
    'npcap_scoped_interfaces_open_failed',
    'prelaunch_host_invalid',
    'prelaunch_host_scope_invalid',
    'private_spool_missing',
    'unsupported_npcap_linktype',
    'windows_required',
}
FAILURE_STAGE = 'startup'


class Header(ct.Structure):
    _fields_ = [('seconds', ct.c_int32), ('microseconds', ct.c_int32),
                ('captured', ct.c_uint32), ('original', ct.c_uint32)]


class Bpf(ct.Structure):
    _fields_ = [('length', ct.c_uint32), ('instructions', ct.c_void_p)]


def block(kind, data):
    data += b'\0' * (-len(data) % 4)
    total = 12 + len(data)
    return struct.pack('<II', kind, total) + data + struct.pack('<I', total)


def packet_block(micros, data, original, interface_id=0):
    return block(6, struct.pack('<IIIII', interface_id, micros >> 32, micros & 0xffffffff,
                                len(data), original) + data)


def ethernet_record(linktype, data, original):
    """Convert only validated Npcap pseudo/raw IP headers to Ethernet framing."""
    if linktype == 1:
        return data, original
    offset = 0
    family = None
    if linktype in (0, 108):
        if len(data) < 5 or original < 5:
            raise ValueError('npcap_pseudo_header_invalid')
        family = struct.unpack_from('!I' if linktype == 108 else '<I', data)[0]
        offset = 4
    elif linktype not in (12, 101):
        raise ValueError('unsupported_npcap_linktype')
    version = data[offset] >> 4 if len(data) > offset else 0
    if family is not None and not (
        (family == 2 and version == 4)
        or (family in (10, 23, 24, 28, 30) and version == 6)
    ):
        raise ValueError('npcap_address_family_mismatch')
    if version not in (4, 6):
        raise ValueError('npcap_ip_version_invalid')
    ether_type = 0x0800 if version == 4 else 0x86DD
    return b'\0' * 12 + struct.pack('!H', ether_type) + data[offset:], original - offset + 14


def _validated_hosts(values, *, required=False):
    if required and not 1 <= len(values) <= 8 or not required and len(values) > 8:
        raise ValueError('prelaunch_host_scope_invalid')
    parsed = []
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError('prelaunch_host_invalid') from error
        if address.is_loopback or address.is_multicast or address.is_unspecified:
            raise ValueError('prelaunch_host_invalid')
        parsed.append(str(address))
    return sorted(set(parsed))


def game_filter(scope, seed_hosts=()):
    tcp = scope.get('tcp') or []
    udp = scope.get('udp') or []
    if isinstance(tcp, dict):
        tcp = [tcp]
    if isinstance(udp, int):
        udp = [udp]
    hosts = set(_validated_hosts(seed_hosts))
    if not scope.get('process_count') or not tcp and not udp and not hosts:
        raise ValueError('game_not_running_or_loopback_sockets_missing')
    if len(tcp) > 64 or len(udp) > 64:
        raise ValueError('game_socket_scope_too_large')
    def port(v):
        if type(v) is not int or not 1 <= v <= 65535:
            raise ValueError('invalid_socket_port')
        return v
    pairs = set()
    for row in tcp:
        local, peer = port(row['local']), port(row['peer'])
        pairs.add((local, peer))
        # Exact local ports are ephemeral. Retain the narrow first-party peer
        # for TLS/443 so a new process-owned connection is captured from SYN.
        if peer == 443 and row.get('address'):
            hosts.update(_validated_hosts([row['address']]))
    pairs = sorted(pairs)
    ports = sorted({port(v) for v in udp})
    parts = []
    if pairs:
        parts.append('(tcp and (' + ' or '.join(
            f'((src port {a} and dst port {b}) or (src port {b} and dst port {a}))'
            for a,b in pairs) + '))')
    if hosts:
        parts.append('(tcp and port 443 and (' + ' or '.join(
            f'host {value}' for value in sorted(hosts)) + '))')
    if ports:
        parts.append('(udp and (' + ' or '.join(f'port {v}' for v in ports) + '))')
    return '(' + ' or '.join(parts) + ')', len(pairs), len(ports)


def prelaunch_filter(hosts):
    """Build a narrow pre-launch filter for already observed first-party peers."""
    unique = _validated_hosts(hosts, required=True)
    return '(tcp and (' + ' or '.join(f'host {value}' for value in unique) + '))', len(unique)


def current_game_scope():
    command = r"""
$aionIds=@(Get-Process | Where-Object {$_.ProcessName -match '(?i)^AION2(?:$|[-_])'} | Select-Object -ExpandProperty Id)
$aionTcp=@(Get-NetTCPConnection -ErrorAction SilentlyContinue | Where-Object {$_.OwningProcess -in $aionIds -and $_.State -in 'Established','SynSent'} | ForEach-Object {@{local=[int]$_.LocalPort;peer=[int]$_.RemotePort;address=[string]$_.RemoteAddress}})
$aionUdp=@(Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object {$_.OwningProcess -in $aionIds} | Select-Object -ExpandProperty LocalPort -Unique | ForEach-Object {[int]$_})
@{process_count=$aionIds.Count;tcp=$aionTcp;udp=$aionUdp}|ConvertTo-Json -Depth 4 -Compress
"""
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                            capture_output=True, timeout=35, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError('game_scope_query_failed')
    return json.loads(result.stdout)


ACCELERATOR_MARKERS = (
    'tap-windows', 'tapwin', 'tun', 'wireguard', 'exitlag', 'netease uu',
    'uu booster', 'uu game', 'uu tunnel', 'razer cortex', 'gearup',
    'lagofast', 'clash', 'mihomo', 'wg-', 'openvpn', 'vpn', 'tunnel',
    'tailscale', 'zerotier', 'sing-box', 'proxifier', 'nordlynx', 'hamachi',
)


def adapter_kind(name, description):
    text = f'{name} {description}'.lower()
    return 'accelerator' if any(marker in text for marker in ACCELERATOR_MARKERS) else 'physical'


def active_devices(loopback_only=False):
    if loopback_only:
        return [dict(device=r'\Device\NPF_Loopback',kind='loopback')]
    command = r"""
$devices=@(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {$_.Status -eq 'Up'} | ForEach-Object {@{device='\Device\NPF_'+$_.InterfaceGuid.ToString().ToUpperInvariant();name=[string]$_.Name;description=[string]$_.InterfaceDescription}})
$devices | ConvertTo-Json -Depth 3 -Compress
"""
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                            capture_output=True, timeout=35, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError('capture_device_query_failed')
    devices = json.loads(result.stdout)
    if isinstance(devices, dict):
        devices = [devices]
    if not isinstance(devices, list) or len(devices) > 31:
        raise ValueError('capture_device_scope_invalid')
    answer = [dict(device=r'\Device\NPF_Loopback',kind='loopback')]
    seen = {answer[0]['device']}
    for item in devices:
        if not isinstance(item, dict) or set(item) != {'device','name','description'}:
            raise ValueError('capture_device_scope_invalid')
        device,name,description=item['device'],item['name'],item['description']
        if (not all(isinstance(value,str) for value in (device,name,description))
                or not device.startswith('\\Device\\NPF_') or len(device) > 96
                or len(name) > 256 or len(description) > 512):
            raise ValueError('capture_device_scope_invalid')
        if device not in seen:
            answer.append(dict(device=device,kind=adapter_kind(name,description)))
            seen.add(device)
    order = {'loopback':0,'accelerator':1,'physical':2}
    return sorted(answer,key=lambda item:order[item['kind']])


def library():
    if os.name != 'nt':
        raise ValueError('windows_required')
    directory = Path(os.environ['SystemRoot']) / 'System32' / 'Npcap'
    global DLL_DIRECTORY
    DLL_DIRECTORY = os.add_dll_directory(str(directory))
    dll = ct.CDLL(str(directory / 'wpcap.dll'))
    signatures = {
        'pcap_open_live': ([ct.c_char_p,ct.c_int,ct.c_int,ct.c_int,ct.c_char_p],ct.c_void_p),
        'pcap_datalink': ([ct.c_void_p],ct.c_int),
        'pcap_compile': ([ct.c_void_p,ct.POINTER(Bpf),ct.c_char_p,ct.c_int,ct.c_uint32],ct.c_int),
        'pcap_setfilter': ([ct.c_void_p,ct.POINTER(Bpf)],ct.c_int),
        'pcap_freecode': ([ct.POINTER(Bpf)],None),
        'pcap_setnonblock': ([ct.c_void_p,ct.c_int,ct.c_char_p],ct.c_int),
        'pcap_next_ex': ([ct.c_void_p,ct.POINTER(ct.POINTER(Header)),ct.POINTER(ct.POINTER(ct.c_ubyte))],ct.c_int),
        'pcap_stats': ([ct.c_void_p,ct.c_void_p],ct.c_int),
        'pcap_close': ([ct.c_void_p],None),
    }
    for name,(args,result) in signatures.items():
        fn=getattr(dll,name)
        fn.argtypes,fn.restype=args,result
    return dll


def replace_filter(dll, item, expression):
    """Atomically replace one Npcap handle filter after compiling it."""
    program = Bpf()
    handle = item['handle']
    if dll.pcap_compile(handle, ct.byref(program), expression.encode('ascii'), 1, 0xffffffff):
        return False
    try:
        if dll.pcap_setfilter(handle, ct.byref(program)):
            return False
        if item.get('compiled'):
            dll.pcap_freecode(ct.byref(item['program']))
        item['program'] = program
        item['compiled'] = True
        return True
    finally:
        if item.get('program') is not program:
            dll.pcap_freecode(ct.byref(program))


def capture(mode, seconds, hosts=()):
    global FAILURE_STAGE
    FAILURE_STAGE = 'private_spool'
    root=PRIVATE_ROOT.resolve()
    if not root.is_dir() or PRIVATE_ROOT.is_symlink():
        raise ValueError('private_spool_missing')
    active, stop = root/'npcap-active.json', root/'npcap-stop.request'
    if mode == 'stop':
        if not active.is_file():
            return dict(status='no_active_npcap_capture',raw_exposed=False)
        stop.touch(exist_ok=True)
        return dict(status='stop_requested',raw_exposed=False)
    if active.exists() or (root/'active-session.json').exists() or stop.exists():
        raise ValueError('active_capture_or_stale_stop_present')
    sockets=[]
    FAILURE_STAGE = 'socket_scope'
    if mode == 'canary':
        for family,address in ((socket.AF_INET,'127.0.0.1'),(socket.AF_INET6,'::1')):
            sock=socket.socket(family,socket.SOCK_DGRAM)
            sock.bind((address,0))
            sockets.append(sock)
        expression='udp and ('+' or '.join(f'port {s.getsockname()[1]}' for s in sockets)+')'
        tcp_count,udp_count=0,len(sockets)
    elif mode == 'prelaunch':
        expression,host_count=prelaunch_filter(hosts)
        tcp_count,udp_count=host_count,0
    else:
        expression,tcp_count,udp_count=game_filter(current_game_scope(), hosts)
    FAILURE_STAGE = 'npcap_library'
    dll=library()
    error=ct.create_string_buffer(256)
    handles=[]
    FAILURE_STAGE = 'interface_open'
    for interface in active_devices(mode == 'canary'):
        device=interface['device']
        handle=dll.pcap_open_live(device.encode('ascii'),65535,0,100,error)
        if handle:
            handles.append(dict(device=device,kind=interface['kind'],handle=handle,program=Bpf(),compiled=False,
                                linktype=None,stats=(ct.c_uint32*16)()))
    if not handles:
        raise ValueError('npcap_scoped_interfaces_open_failed')
    identifier=uuid.uuid4().hex
    source=root/('npcap-'+identifier+'.pcapng')
    frames=truncated=0
    first=last=None
    reason='duration'
    try:
        FAILURE_STAGE = 'filter_setup'
        usable=[]
        for item in handles:
            handle=item['handle']
            item['linktype']=dll.pcap_datalink(handle)
            if item['linktype'] not in (0,1,12,101,108):
                dll.pcap_close(handle)
                continue
            if not replace_filter(dll,item,expression) or dll.pcap_setnonblock(handle,1,error):
                if item['compiled']:
                    dll.pcap_freecode(ct.byref(item['program']))
                    item['compiled']=False
                dll.pcap_close(handle)
                continue
            usable.append(item)
        handles=usable
        if not handles:
            raise ValueError('npcap_scoped_filter_setup_failed')
        active.write_text(json.dumps(dict(session_id=identifier,pid=os.getpid(),max_seconds=seconds)),encoding='ascii')
        FAILURE_STAGE = 'packet_recording'
        with source.open('xb') as stream:
            stream.write(block(0x0a0d0d0a,struct.pack('<IHHq',0x1a2b3c4d,1,0,-1)))
            # Every private object uses standard Ethernet framing, which the
            # already-loaded VM route handles without a new analyzer release.
            for _ in handles:
                stream.write(block(1,struct.pack('<HHI',1,0,65535)))
            kinds={kind:sum(item['kind']==kind for item in handles)
                   for kind in ('loopback','accelerator','physical')}
            print(json.dumps(dict(status='recording_npcap_scoped',seconds=seconds,
                  interface_count=len(handles),interface_kind_counts=kinds,
                  tcp_pair_count=tcp_count,udp_port_count=udp_count,routes_changed=False,
                  raw_exposed=False)),flush=True)
            if mode == 'canary':
                for sock in sockets:
                    for _ in range(3):
                        sock.sendto(b'AIONLAB-CANARY-v1',sock.getsockname())
            started=time.monotonic()
            filter_expression=expression
            filter_updates=0
            filter_errors=0
            next_filter_refresh=started+2.0
            while time.monotonic()-started < seconds:
                if stop.exists():
                    reason='owner_stop'
                    break
                now=time.monotonic()
                if mode == 'game' and now >= next_filter_refresh:
                    next_filter_refresh=now+2.0
                    try:
                        refreshed,refreshed_tcp,refreshed_udp=game_filter(current_game_scope(),hosts)
                        if refreshed != filter_expression:
                            if not all(replace_filter(dll,item,refreshed) for item in handles):
                                raise ValueError('npcap_dynamic_filter_update_failed')
                            filter_expression=refreshed
                            tcp_count,udp_count=refreshed_tcp,refreshed_udp
                            filter_updates+=1
                        filter_errors=0
                    except (ValueError, OSError, subprocess.SubprocessError):
                        filter_errors+=1
                        if filter_errors >= 3:
                            raise ValueError('npcap_dynamic_filter_update_failed')
                observed=False
                for interface_id,item in enumerate(handles):
                    header=ct.POINTER(Header)()
                    payload=ct.POINTER(ct.c_ubyte)()
                    status=dll.pcap_next_ex(item['handle'],ct.byref(header),ct.byref(payload))
                    if status == 0:
                        continue
                    if status != 1:
                        raise ValueError('npcap_read_failed')
                    observed=True
                    h=header.contents
                    if not 0 < h.captured <= 65535 or h.original < h.captured or not 0 <= h.microseconds < 1000000:
                        raise ValueError('npcap_capture_header_invalid')
                    timestamp=h.seconds*1000000+h.microseconds
                    frame,wire=ethernet_record(item['linktype'],ct.string_at(payload,h.captured),h.original)
                    encoded=packet_block(timestamp,frame,wire,interface_id)
                    if stream.tell()+len(encoded) > MAX_FILE:
                        reason='file_budget'
                        break
                    stream.write(encoded)
                    frames+=1
                    item['packet_count']=item.get('packet_count',0)+1
                    truncated+=h.captured < h.original
                    first=timestamp if first is None else min(first,timestamp)
                    last=timestamp if last is None else max(last,timestamp)
                if reason == 'file_budget':
                    break
                if not observed:
                    time.sleep(.01)
        FAILURE_STAGE = 'capture_finalize'
        stats_available=True
        driver_drops=interface_drops=0
        for item in handles:
            if dll.pcap_stats(item['handle'],ct.byref(item['stats'])):
                stats_available=False
            else:
                driver_drops += int(item['stats'][1])
                interface_drops += int(item['stats'][2])
    finally:
        for item in handles:
            if item['compiled']:
                dll.pcap_freecode(ct.byref(item['program']))
            dll.pcap_close(item['handle'])
        for sock in sockets:
            sock.close()
        if active.is_file():
            active.unlink()
        if stop.is_file():
            stop.unlink()
    hasher=hashlib.sha256()
    with source.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            hasher.update(chunk)
    packets_by_kind={kind:sum(item.get('packet_count',0) for item in handles if item['kind']==kind)
                     for kind in ('loopback','accelerator','physical')}
    return dict(status='capture_stopped',mode=mode,capture_sha256=hasher.hexdigest(),
                size_bytes=source.stat().st_size,packet_headers_seen=frames,
                interface_count=len(handles),packet_headers_by_kind=packets_by_kind,
                truncated_frames=truncated,first_unix_ms=first//1000 if first else None,
                last_unix_ms=last//1000 if last else None,stop_reason=reason,
                driver_drops=driver_drops if stats_available else None,
                interface_drops=interface_drops if stats_available else None,
                dynamic_filter_updates=filter_updates if mode == 'game' else 0,
                routes_changed=False,raw_exposed=False,paths_exposed=False)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=('canary','game','prelaunch','stop'),required=True)
    parser.add_argument('--seconds',type=int,default=180)
    parser.add_argument('--host',action='append',default=[])
    args=parser.parse_args()
    if not 3 <= args.seconds <= 300:
        raise SystemExit('capture_duration_out_of_range')
    try:
        print(json.dumps(capture(args.mode,args.seconds,args.host)),flush=True)
    except (ValueError,OSError,subprocess.SubprocessError,json.JSONDecodeError) as error:
        code = str(error)
        if code not in SAFE_ERROR_CODES:
            if isinstance(error, PermissionError):
                code = 'npcap_permission_denied'
            elif isinstance(error, FileNotFoundError):
                code = 'npcap_dependency_missing'
            elif isinstance(error, subprocess.SubprocessError):
                code = 'npcap_scope_subprocess_failed'
            elif isinstance(error, json.JSONDecodeError):
                code = 'npcap_scope_json_invalid'
            else:
                code = 'npcap_io_failed'
        print(json.dumps(dict(status='failed',error=code,stage=FAILURE_STAGE,
              raw_exposed=False,paths_exposed=False)),flush=True)
        raise SystemExit(2)
