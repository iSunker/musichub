import subprocess, json, base64, hashlib
from Crypto.Cipher import AES

cmd = [
    r'C:\WINDOWS\System32\OpenSSH\ssh.exe', '-p', '36688', '-i', 'C:/Users/艾森克/.ssh/id_ed25519',
    'iSunker@192.168.0.7',
    'cat /volume2/docker_ssd/cookiecloud/data/浏览器端信息.txt'
]
info_text = subprocess.check_output(cmd).decode('utf-8')
lines = [l.strip() for l in info_text.splitlines() if l.strip()]

uuid = ''
password = ''
for i, l in enumerate(lines):
    if '用户 KEY' in l or 'UUID' in l.upper():
        uuid = lines[i+1]
    elif '端对端' in l or '密码' in l:
        password = lines[i+1]

cat_json = [
    r'C:\WINDOWS\System32\OpenSSH\ssh.exe', '-p', '36688', '-i', 'C:/Users/艾森克/.ssh/id_ed25519',
    'iSunker@192.168.0.7',
    f'cat /volume2/docker_ssd/cookiecloud/data/{uuid}.json'
]
encrypted_json_str = subprocess.check_output(cat_json).decode('utf-8')
encrypted = json.loads(encrypted_json_str).get('encrypted', '')
key16 = hashlib.md5(f'{uuid}-{password}'.encode()).hexdigest()[:16]
raw = base64.b64decode(encrypted)
salt = raw[8:16]
d, prev = b'', b''
while len(d) < 48:
    prev = hashlib.md5(prev + key16.encode() + salt).digest()
    d += prev
cipher = AES.new(d[:32], AES.MODE_CBC, d[32:48])
plain = cipher.decrypt(raw[16:])
plain = plain[:-plain[-1]]
cookie_data = json.loads(plain.decode('utf-8')).get('cookie_data', {})

def get_c(dom):
    return '; '.join([x['name']+'='+x['value'] for x in cookie_data.get(dom, [])])

tang_c = get_c('www.tangpt.top')
muxue_c = get_c('pt.muxuege.org')

cat_env = [
    r'C:\WINDOWS\System32\OpenSSH\ssh.exe', '-p', '36688', '-i', 'C:/Users/艾森克/.ssh/id_ed25519',
    'iSunker@192.168.0.7',
    'cat /volume2/docker_ssd/opencd2others/data/.env'
]
cur_env = subprocess.check_output(cat_env).decode('utf-8')

new_lines = []
if 'TANGPT_COOKIE' not in cur_env:
    new_lines.append(f'\nTANGPT_URL=https://www.tangpt.top\nTANGPT_COOKIE={tang_c}\nTANGPT_MUSIC_CATEGORY=409\nTANGPT_PUBLISH_CATEGORY=TangPT\nTANGPT_MAX_PER_CYCLE=1\nENABLE_PUBLISH_TANGPT=false\nTANGPT_INTERVAL=1800\n')

if 'MUXUEGE_COOKIE' not in cur_env:
    new_lines.append(f'\nMUXUEGE_URL=https://pt.muxuege.org\nMUXUEGE_COOKIE={muxue_c}\nMUXUEGE_MUSIC_CATEGORY=408\nMUXUEGE_PUBLISH_CATEGORY=MuXueGe\nMUXUEGE_MAX_PER_CYCLE=1\nENABLE_PUBLISH_MUXUEGE=false\nMUXUEGE_INTERVAL=1800\n')

if new_lines:
    append_content = ''.join(new_lines).encode('utf-8')
    p = subprocess.Popen([
        r'C:\WINDOWS\System32\OpenSSH\ssh.exe', '-p', '36688', '-i', 'C:/Users/艾森克/.ssh/id_ed25519',
        'iSunker@192.168.0.7',
        'cat >> /volume2/docker_ssd/opencd2others/data/.env'
    ], stdin=subprocess.PIPE)
    p.communicate(input=append_content)
    print('Appended TANGPT and MUXUEGE configs to .env')
else:
    print('Configs already exist in .env')
