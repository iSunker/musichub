import subprocess

files = ['config.py', 'daemon.py', 'db.py', 'hdkylin_site.py', 'tangpt_site.py', 'muxuege_site.py', 'publish.py']
for f in files:
    local_p = f'd:/Projects/PT-Project/opencd2others/src/{f}'
    with open(local_p, 'rb') as fp:
        content = fp.read()
    cmd = [
        r'C:\WINDOWS\System32\OpenSSH\ssh.exe', '-p', '36688', '-i', 'C:/Users/艾森克/.ssh/id_ed25519',
        'iSunker@192.168.0.7',
        f'/usr/local/bin/docker exec -i opencd2musopia sh -c "cat > /app/{f}"'
    ]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(input=content)
    print(f'Transferred {f}: {len(content)} bytes, err={err.decode("utf-8", errors="ignore").strip()}')
