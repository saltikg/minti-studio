# wsgi.py
# Paket-isim çakışmalarını atlatmak için root'taki siteapp.py içinden app'i yükle
from siteapp import app
from app import create_app
app = create_app()
