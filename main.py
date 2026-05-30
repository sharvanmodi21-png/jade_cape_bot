# Entry point for Railway deployment
import subprocess
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
subprocess.run([sys.executable, "jade_cape_bot.py"], check=True)
