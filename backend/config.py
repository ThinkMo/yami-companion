from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./yami_dev.db"
    pairing_code_ttl_seconds: int = 600
    session_ttl_seconds: int = 1800
    jwt_secret: str = "yami-dev-jwt-secret-change-in-prod"
    jwt_ttl_seconds: int = 86400 * 14

    # Agent Harness provider routing: auto | volc_plan | tencent_plan | doubao_seed | deepseek
    agent_provider: str = "auto"

    # Volcengine Ark Agent Plan / Coding Plan (OpenAI-compatible /chat/completions)
    volc_plan_api_key: str = ""
    volc_plan_base_url: str = "https://ark.cn-beijing.volces.com/api/plan/v3"
    volc_plan_model: str = "doubao-seed-2.1-pro"

    # Tencent Cloud Token Plan / Agent Plan (OpenAI-compatible /chat/completions)
    tencent_plan_api_key: str = ""
    tencent_plan_base_url: str = "https://tokenhub.tencentmaas.com/plan/v3"
    tencent_plan_model: str = "deepseek-flash"

    # DeepSeek API
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_chat_model: str = "deepseek-chat"
    deepseek_reasoner_model: str = "deepseek-reasoner"

    # Volcengine Ark (Doubao Seed Responses API + Multimodal Audio Understanding)
    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_seed_model: str = "doubao-seed-2.0"
    ark_audio_model: str = ""
    ark_embedding_model: str = "doubao-embedding-text"

    # Tencent Cloud Real-Time TTS WebSocket (TextToStreamAudioWS)
    tencent_app_id: int = 0
    tencent_secret_id: str = ""
    tencent_secret_key: str = ""
    tencent_tts_voice_type: int = 101001

    # Neo4j / Graphiti
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "yami_dev_password"

    model_config = {"env_prefix": "YAMI_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
