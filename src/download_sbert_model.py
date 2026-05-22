# download_sbert_model.py
from sentence_transformers import SentenceTransformer

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

print(f"📥 Downloading {MODEL_NAME} ...")
model = SentenceTransformer(MODEL_NAME, cache_folder="./models")
print(f"✅ Model downloaded and cached in ./models")
