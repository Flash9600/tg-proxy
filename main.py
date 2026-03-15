import requests
import re
import socket
import concurrent.futures
import time
from datetime import datetime
import json
import os
import argparse
import asyncio
try:
    from telethon import TelegramClient
    from telethon.connection import ConnectionTcpMTProxyRandomizedIntermediate
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
    print("⚠️ Telethon not installed. Using TCP ping only (pip install telethon for MTProto check)")

# API ключи для Telethon (получи на my.telegram.org)
API_ID = None  # Вставь свой api_id
API_HASH = None  # Вставь свой api_hash

SOURCES = [
    "https://raw.githubusercontent.com/SoliSpirit/mtproto/master/all_proxies.txt",
    "https://raw.githubusercontent.com/Grim1313/mtproto-for-telegram/refs/heads/master/all_proxies.txt",
    "https://raw.githubusercontent.com/ALIILAPRO/MTProtoProxy/main/mtproto.txt",
    "https://raw.githubusercontent.com/yemixzy/proxy-projects/main/proxies/mtproto.txt",
    "https://mtpro.xyz/api/?type=mtproto",  # + MTPro.XYZ API
    "https://mtpro.xyz/api/?type=mtproto-ru",  # RU только
]

TIMEOUT = 2.0
MAX_WORKERS = 100

RU_DOMAINS = [
    '.ru', 'yandex', 'vk.com', 'mail.ru', 'ok.ru', 'dzen', 'rutube',
    'sber', 'tinkoff', 'vtb', 'gosuslugi', 'nalog', 'mos.ru',
    'ozon', 'wildberries', 'avito', 'kinopoisk', 'mts', 'beeline'
]

BLOCKED = ['instagram', 'facebook', 'twitter', 'bbc', 'meduza', 'linkedin', 'torproject']

def get_proxies_from_text(text: str):
    proxies = set()

    tg_pattern = re.compile(r'tg://proxy\?server=([^&\s]+)&port=(\d+)&secret=([A-Za-z0-9_=-]+)', re.IGNORECASE)
    for h, p, s in tg_pattern.findall(text):
        proxies.add((h, int(p), s))

    tme_pattern = re.compile(r't\.me/proxy\?server=([^&\s]+)&port=(\d+)&secret=([A-Za-z0-9_=-]+)', re.IGNORECASE)
    for h, p, s in tme_pattern.findall(text):
        proxies.add((h, int(p), s))

    simple_pattern = re.compile(r'([a-zA-Z0-9\.-]+):(\d+):([A-Fa-f0-9]{16,})')
    for h, p, s in simple_pattern.findall(text):
        proxies.add((h, int(p), s))

    txt = text.strip()
    if txt.startswith('[') or txt.startswith('{'):
        try:
            data = json.loads(txt)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        host = item.get('host') or item.get('server')
                        port = item.get('port')
                        secret = item.get('secret')
                        if host and port and secret:
                            proxies.add((host, int(port), str(secret)))
        except Exception:
            pass

    return proxies

def decode_domain(secret: str):
    if not secret.startswith('ee'):
        return None
    try:
        chars = []
        for i in range(2, len(secret), 2):
            val = int(secret[i:i + 2], 16)
            if val == 0:
                break
            chars.append(chr(val))
        return "".join(chars).lower()
    except Exception:
        return None

async def check_proxy_telethon(p):
    """Реальная MTProto проверка через Telethon"""
    if not TELETHON_AVAILABLE or not API_ID or not API_HASH:
        return None

    host, port, secret = p
    domain = decode_domain(secret)

    if len(secret) < 16 or (domain and any(b in domain for b in BLOCKED)):
        return None

    client = TelegramClient(
        f'test_{host.replace(".", "_")}_{port}', API_ID, API_HASH,
        connection=ConnectionTcpMTProxyRandomizedIntermediate,
        proxy=(host, int(port), secret),
        timeout=8.0
    )

    try:
        start = time.time()
        await client.connect()
        await client.get_config()  # Проверяем связь без авторизации
        ping = time.time() - start

        region = 'eu'
        if domain:
            for r in RU_DOMAINS:
                if r in domain:
                    region = 'ru'
                    break

        await client.disconnect()
        return {
            'host': host, 'port': port, 'secret': secret,
            'link': f"tg://proxy?server={host}&port={port}&secret={secret}",
            'ping': ping, 'region': region, 'method': 'Telethon_OK'
        }
    except Exception:
        await client.disconnect()
        return None

def check_proxy_tcp(p):
    """Fallback TCP проверка (текущая логика)"""
    host, port, secret = p
    domain = decode_domain(secret)

    if len(secret) < 16 or (domain and any(b in domain for b in BLOCKED)):
        return None

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        start = time.time()
        s.connect((host, port))
        ping = time.time() - start
        s.close()
    except Exception:
        return None

    region = 'eu'
    if domain:
        for r in RU_DOMAINS:
            if r in domain:
                region = 'ru'
                break

    return {
        'host': host, 'port': port, 'secret': secret,
        'link': f"tg://proxy?server={host}&port={port}&secret={secret}",
        'ping': ping, 'region': region, 'method': 'TCP_OK'
    }

def make_tme_link(host, port, secret):
    return f"https://t.me/proxy?server={host}&port={port}&secret={secret}"

async def main_async(args):
    start_time = time.time()
    print("🚀 MTProto Proxy Collector v2.0")

    # Создаём папку verified
    os.makedirs('verified', exist_ok=True)

    print("📥 Collecting proxies...")
    all_raw = set()

    for url in SOURCES:
        name = url.split('/')[-1] or url.split('/')[-2]
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                print(f"✗ {name} -> HTTP {r.status_code}")
                continue
            extracted = get_proxies_from_text(r.text)
            all_raw.update(extracted)
            print(f"✓ {name} -> +{len(extracted)}")
        except Exception as e:
            print(f"✗ {name}: {e}")

    print(f"
⚡ Checking {len(all_raw)} unique proxies...")

    # Проверяем батчами по 50 (чтобы не перегружать)
    valid = []
    batch_size = 50

    if TELETHON_AVAILABLE and API_ID and API_HASH:
        print("🔥 Using Telethon MTProto check")
        semaphore = asyncio.Semaphore(10)  # 10 одновременных

        async def check_batch(batch):
            tasks = []
            for p in batch:
                async def check_p(p=p):
                    async with semaphore:
                        return await check_proxy_telethon(p)
                tasks.append(check_p())
            return await asyncio.gather(*tasks)

        for i in range(0, len(all_raw), batch_size):
            batch = list(all_raw)[i:i+batch_size]
            results = await check_batch(batch)
            valid.extend([r for r in results if r])
            print(f"  {i+len([r for r in results if r])}/{i+batch_size}")
    else:
        print("📡 Using TCP ping (install telethon for full MTProto check)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as exc:
            futures = {exc.submit(check_proxy_tcp, p): p for p in all_raw}
            for f in concurrent.futures.as_completed(futures):
                res = f.result()
                if res:
                    valid.append(res)

    ru = sorted([x for x in valid if x['region'] == 'ru'], key=lambda x: x['ping'])
    eu = sorted([x for x in valid if x['region'] == 'eu'], key=lambda x: x['ping'])

    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')

    # Сохраняем в verified/
    files = {
        'verified/proxy_ru_verified.txt': ru,
        'verified/proxy_eu_verified.txt': eu,
        'verified/proxy_all_verified.txt': valid
    }

    for filename, proxies_list in files.items():
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(f"# Verified {'RU' if 'ru' in filename else 'EU' if 'eu' in filename else 'All'} Proxies ({len(proxies_list)})\n")
            f.write(f"# Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
            f.write(f"# Method: {proxies_list[0]['method'] if proxies_list else 'N/A'}\n\n")
            f.write('\n'.join([x['link'] for x in proxies_list[:args.top] if args.top else proxies_list]))

    # t.me формат
    with open('verified/proxy_all_tme_verified.txt', 'w', encoding='utf-8') as f:
        f.write(f"# Verified Proxies t.me format ({len(valid)})\n")
        f.write(f"# Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")
        for x in valid[:args.top] if args.top else valid:
            f.write(make_tme_link(x['host'], x['port'], x['secret']) + '\n')

    # Расширенная статистика
    stats = {
        'timestamp_utc': datetime.utcnow().isoformat(),
        'total_raw': len(all_raw),
        'total_verified': len(valid),
        'ru_count': len(ru),
        'eu_count': len(eu),
        'telethon_used': TELETHON_AVAILABLE and bool(API_ID and API_HASH),
        'best_ru_ping': round(ru[0]['ping'], 3) if ru else None,
        'best_eu_ping': round(eu[0]['ping'], 3) if eu else None,
        'execution_time': round(time.time() - start_time, 1),
        'sources': len(SOURCES)
    }

    with open('verified/proxy_stats_verified.json', 'w') as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    print(f"
✅ VERIFIED: RU={len(ru)}, EU={len(eu)}, TOTAL={len(valid)}")
    print(f"📁 Saved to verified/ folder")
    print(f"⏱️ Time: {stats['execution_time']}s")

def main():
    parser = argparse.ArgumentParser(description='🚀 Advanced MTProto Proxy Collector')
    parser.add_argument('--timeout', type=float, default=2.0, help='TCP timeout (s)')
    parser.add_argument('--workers', type=int, default=100, help='TCP workers count')
    parser.add_argument('--top', type=int, default=0, help='Save only TOP N fastest (0=all)')
    args = parser.parse_args()

    global TIMEOUT
    TIMEOUT = args.timeout

    asyncio.run(main_async(args))

if __name__ == "__main__":
    main()

