import time
from musichub.routing.graph import RoutingGraphManager
from musichub.publisher.engine import PublisherEngine
from musichub.publisher.delayed_queue import DelayedReaddQueue
from musichub.core.qb_client import QBClient

graph_mgr = RoutingGraphManager(db_path='/app/musichub/data/musichub.db')
publish_qb = QBClient('192.168.0.7', 3010)
engine = PublisherEngine(publish_qb, graph_mgr)
delayed_queue = DelayedReaddQueue(db_path='/app/musichub/data/musichub.db', engine=engine)

print('=== 延迟回加监控进程已启动 ===', flush=True)
while True:
    try:
        res = delayed_queue.process_pending_readds(max_tasks=5)
        if res:
            print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] 成功回加做种任务:', res, flush=True)
    except Exception as e:
        print(f'监控异常: {e}', flush=True)
    time.sleep(15)
