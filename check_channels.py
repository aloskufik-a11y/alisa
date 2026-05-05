import urllib.request
import re

channels = [
    'mrkt_alerts', 'tgmrkt_alerts', 'getgems_sales', 'mrkt_sales', 
    'nft_alerts', 'GiftsMonitor', 'tg_gifts_monitor', 'mrkt_gifts'
]

for channel in channels:
    try:
        req = urllib.request.Request(f'https://t.me/s/{channel}', headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req).read().decode('utf-8')
        links = re.findall(r'href="(https://t\.me/mrkt[^"]+)"', html)
        if links:
            print(f'--- {channel} ---')
            for l in list(set(links))[:5]:
                print(l)
    except Exception as e:
        print(f"Error {channel}: {e}")
