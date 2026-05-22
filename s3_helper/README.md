# Minti S3 Helper

Bu kucuk Flask yardimcisi sadece S3 degiskenlerini `.env` dosyasindan okuyup
istenen dosyayi localhost uzerinden indirmek icin hazirlandi.

## Kurulum

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Calistirma

```bash
flask --app app run --host 127.0.0.1 --port 5055
```

## Indirme

```bash
curl http://127.0.0.1:5055/download
```
