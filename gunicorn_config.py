import os

# Bind to 0.0.0.0 on Render's assigned port (falls back to 5000 locally)
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

# Use 2 workers (enough for this small API; adjust for your plan if needed)
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
