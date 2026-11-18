# -*- coding: utf-8 -*-
import subprocess
import time
import os

base_dir = r"d:\Projects\PT-Project\opencd2others\src"
gc_file = os.path.join(base_dir, "opencd_gc.py")
orphan_file = os.path.join(base_dir, "music_orphan_gc.py")
daemon_file = os.path.join(base_dir, "daemon.py")

with open(gc_file, "rb") as f:
    gc_bytes = f.read()
with open(orphan_file, "rb") as f:
    orphan_bytes = f.read()
with open(daemon_file, "rb") as f:
    daemon_bytes = f.read()

print("1. 执行 docker compose up -d --force-recreate musopia 以重载新 .env...")
recreate_cmd = [
    "ssh", "-p", "36688", "-i", "C:/Users/艾森克/.ssh/id_ed25519", "iSunker@192.168.0.7",
    "cd /volume2/docker_ssd/opencd2others && /usr/local/bin/docker compose up -d --force-recreate musopia"
]
res_up = subprocess.run(recreate_cmd, capture_output=True, text=True, encoding="utf-8")
print(res_up.stdout)
if res_up.stderr:
    print(res_up.stderr)

# 2. 将最新脚本直传并 copy 进容器
print("2. 将最新脚本传进容器 /app/...")
def push_to_container(data, remote_tmp, container_dest):
    p = subprocess.Popen([
        "ssh", "-p", "36688", "-i", "C:/Users/艾森克/.ssh/id_ed25519", "iSunker@192.168.0.7",
        f"cat > {remote_tmp} && /usr/local/bin/docker cp {remote_tmp} opencd2musopia:{container_dest}"
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(input=data)
    if p.returncode != 0:
        print(f"❌ 传输 {container_dest} 失败:", err.decode("utf-8", "replace"))
    else:
        print(f"✅ 传输 {container_dest} 成功")

push_to_container(gc_bytes, "/tmp/_opencd_gc.py", "/app/opencd_gc.py")
push_to_container(orphan_bytes, "/tmp/_music_orphan_gc.py", "/app/music_orphan_gc.py")
push_to_container(daemon_bytes, "/tmp/_daemon.py", "/app/daemon.py")

# 3. 重启容器使新代码生效
print("3. 重启容器生效...")
restart_cmd = [
    "ssh", "-p", "36688", "-i", "C:/Users/艾森克/.ssh/id_ed25519", "iSunker@192.168.0.7",
    "/usr/local/bin/docker restart opencd2musopia"
]
subprocess.run(restart_cmd, capture_output=True, text=True, encoding="utf-8")
time.sleep(3)

# 4. 回读环境变量与 daemon 状态
print("4. 回读验证新环境变量与启动日志...")
verify_cmd = [
    "ssh", "-p", "36688", "-i", "C:/Users/艾森克/.ssh/id_ed25519", "iSunker@192.168.0.7",
    "/usr/local/bin/docker exec opencd2musopia printenv ENABLE_GC && "
    "/usr/local/bin/docker exec opencd2musopia printenv GC_INTERVAL && "
    "/usr/local/bin/docker logs --tail 25 opencd2musopia"
]
res_ver = subprocess.run(verify_cmd, capture_output=True, text=True, encoding="utf-8")
print("STDOUT:\n", res_ver.stdout)
if res_ver.stderr:
    print("STDERR:\n", res_ver.stderr)
