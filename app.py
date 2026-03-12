"""
Application entry point for Render deployment.
This wrapper imports the Flask app from Irres_api-main/app.py
"""

import sys
import os

# Add the Irres_api-main directory to Python path so we can import from it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'Irres_api-main'))

from app import app

if __name__ == "__main__":
    app.run()
