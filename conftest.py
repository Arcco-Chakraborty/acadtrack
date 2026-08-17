"""Put the project root on sys.path so tests can import app/db/schedule."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
