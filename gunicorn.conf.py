bind = "127.0.0.1:8080"
workers = 1
worker_class = "gthread"
threads = 100

timeout = 120
graceful_timeout = 10
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = "info"
