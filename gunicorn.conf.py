def post_worker_init(worker):
    from app import start_worker
    start_worker()
