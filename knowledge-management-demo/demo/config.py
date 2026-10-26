import os
from dataclasses import dataclass


def load_model_env(path):
    """Read only model settings, including the existing Graphiti env aliases."""
    from dotenv import dotenv_values
    values = dotenv_values(path)
    aliases = {
        "LLM_API_KEY": ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"),
        "LLM_BASE_URL": ("LLM_BASE_URL", "OPENAI_BASE_URL", "DEEPSEEK_BASE_URL"),
        "LLM_MODEL": ("LLM_MODEL", "GRAPHITI_LLM_MODEL", "DEEPSEEK_MODEL"),
        "EMBEDDING_API_KEY": ("EMBEDDING_API_KEY", "GRAPHITI_EMBEDDING_API_KEY"),
        "EMBEDDING_ENDPOINT": ("EMBEDDING_ENDPOINT", "GRAPHITI_EMBEDDING_BASE_URL"),
        "EMBEDDING_MODEL": ("EMBEDDING_MODEL", "GRAPHITI_EMBEDDING_MODEL"),
        "EMBEDDING_DIMENSIONS": ("EMBEDDING_DIMENSIONS",),
    }
    for target, names in aliases.items():
        for name in names:
            if values.get(name):
                os.environ.setdefault(target, values[name])
                break


@dataclass(frozen=True)
class Config:
    uri: str = "bolt://127.0.0.1:17693"
    user: str = "neo4j"
    password: str = "atlas-demo-local"
    database: str = "neo4j"
    dataset: str = "default"
    pending_uri: str = "bolt://127.0.0.1:17695"

    @classmethod
    def from_env(cls):
        return cls(uri=os.getenv("KD_NEO4J_URI", cls.uri), user=os.getenv("KD_NEO4J_USER", cls.user),
                   password=os.getenv("KD_NEO4J_PASSWORD", cls.password),
                   database=os.getenv("KD_NEO4J_DATABASE", cls.database), dataset=os.getenv("KD_DATASET", cls.dataset),
                   pending_uri=os.getenv("KD_PENDING_NEO4J_URI",cls.pending_uri))
