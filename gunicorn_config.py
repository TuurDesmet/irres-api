import multiprocessing

# Bind to 0.0.0.0 on the port Render provides
bind = "0.0.0.0:5000"

# Use 2-4 workers (adjust based on your Render plan)
workers = 2

# Worker class
worker_class = "sync"

# Timeout settings - IMPORTANT for slow scraping requests
timeout = 300  # 5 minutes - allows time for all listings to be fetched
graceful_timeout = 300
keepalive = 5

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Preload app for better performance
preload_app = True