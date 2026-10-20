# gunicorn config for NonNewtonian Physicists. Used by deploy/systemd/nnp.service:
#   gunicorn --config /opt/nnp/deploy/gunicorn.conf.py nonnewtonian.web.wsgi:app
bind = "127.0.0.1:8000"   # loopback ONLY: the ProxyFix trust boundary
workers = 1               # memory:// rate limits and the deck semaphore are per-process; 512MB
worker_class = "gthread"
threads = 8
timeout = 120             # worker heartbeat, NOT a per-request deadline (see photos.FETCH_DEADLINE)
graceful_timeout = 30
keepalive = 5
accesslog = None          # URL paths carry manage/admin tokens; never enable without redaction
errorlog = "-"            # journald
forwarded_allow_ips = "127.0.0.1"
# No max_requests: recycling the only worker resets the in-memory limits;
# the systemd MemoryMax is the leak guard instead.
