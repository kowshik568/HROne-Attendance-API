import os, ssl, sys
from pathlib import Path
from dotenv import load_dotenv
import certifi
print('Python version:', sys.version)
print('OpenSSL version:', ssl.OPENSSL_VERSION)
print('certifi location:', certifi.where())
proj_root = Path(__file__).resolve().parent
load_dotenv(dotenv_path=proj_root / '.env')
uri = os.getenv('MONGO_URI')
print('MONGO_URI loaded?', bool(uri))
from pymongo import MongoClient
try:
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    print('Ping succeeded')
except Exception as e:
    print('Ping failed:', type(e), e)
