import os, sys, json
# 📦 src klasörünü path'e ekle
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.runnables import RunnableSequence
from utils.db_utils import DuckDBHelper
from dotenv import load_dotenv
load_dotenv()

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.7, api_key=OPENAI_KEY)


prompt = PromptTemplate(
    input_variables=["season"],
    template="Summarize the context for the {season} shopping season in one short paragraph."
)

chain = RunnableSequence(first=prompt, last=llm)


class ContextChain:
    """
    ContextChain = Minti'nin B-Katmanı (Context Orchestration)
    Sezon ve trend verisini kullanarak, LLM için context oluşturur.
    """

    def __init__(self, db_path: str = "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb"):
        self.db = DuckDBHelper(db_path)
        self.model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.llm = ChatOpenAI(model=self.model_name, temperature=0.7, api_key=OPENAI_KEY)


    def build_context_payload(self, keyword: str) -> dict:
        """DB'den sezon bilgilerini alır, keyword ile birleştirip LLM'e verilecek payload hazırlar."""
        current_season = self.db.get_current_season()
        context_payload = {
            "keyword": keyword,
            "season": current_season,
            "existing_content": self.db.check_existing_content(keyword, current_season),
        }
        return context_payload

    def generate_context_summary(self, keyword: str) -> str:
        """LangChain kullanarak context özetini oluşturur."""
        payload = self.build_context_payload(keyword)

        if payload["existing_content"]:
            return f"Skipping {keyword}: content already exists for season {payload['season']}"

        template = """
        You are a context planner for an AI content system.
        Your goal is to summarize the creative background for a blog post.
        Input keyword: {keyword}
        Current season: {season}

        Create a short paragraph (80-120 words) describing:
        - Why this topic is relevant now
        - What kind of tone the AI writer should use (educational, festive, luxury, etc.)
        - Key contextual anchors (themes, events, or buying reasons)
        """
        prompt = PromptTemplate(
            input_variables=["keyword", "season"],
            template=template.strip()
        )

        chain = RunnableSequence(first=prompt, last=self.llm)

        # 🔹 .run() değil .invoke() kullanılmalı
        response = chain.invoke({"keyword": payload["keyword"], "season": payload["season"]})
        return response.content if hasattr(response, "content") else str(response)

    def run(self, keyword: str) -> dict:
        """Ana giriş noktası — context oluşturur ve JSON döndürür."""
        summary_text = self.generate_context_summary(keyword)
        payload = self.build_context_payload(keyword)

        result = {
            "keyword": keyword,
            "season": payload["season"],
            "context_summary": summary_text,
        }
        print(json.dumps(result, indent=2))
        return result

    def close(self):
        """DuckDB bağlantısını kapatır."""
        self.db.close()


if __name__ == "__main__":
    chain = ContextChain()
    chain.run("rolex watches")
    chain.close()
