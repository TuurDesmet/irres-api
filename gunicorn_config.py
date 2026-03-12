import os

# Bind to 0.0.0.0 on Render's assigned port (falls back to 5000 locally)
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

# Use 2 workers (enough for this small API; adjust for your plan if needed)
workers = 2

# Worker class
worker_class = "sync"

# Timeout settings - IMPORTANT for slow scraping requests
# Allow up to 15 minutes so the full scrape can complete, even when there are many listings
timeout = 900
graceful_timeout = 900
keepalive = 5

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Preload app for better performance
preload_app = True
