import requests
h = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Cookie': 'c_secure_pass=eyJ1c2VyX2lkIjoxMDc5MiwiZXhwaXJlcyI6MTgxMzc4MzI3NX0uZGViMTEzYjAyZTNkZDc4ZDUzZGQxNTEyMzgzMDk3MzhjYmMyNmEyM2QyZTE3NDFiNjJmYjk4YmQzZmNiOWI1ZA%3D%3D; sl-session=iFlFANpvAGtL3survp86Uw=='
}
for to in [15, 30]:
    try:
        r = requests.get('https://xingwan.cc/upload.php', headers=h, verify=False, timeout=to, allow_redirects=False)
        print('to=' + str(to) + ' -> status=' + str(r.status_code) + ', location=' + str(r.headers.get('Location')) + ', len=' + str(len(r.text)))
        break
    except Exception as e:
        print('to=' + str(to) + ' -> err=' + str(type(e).__name__) + ': ' + str(e))
