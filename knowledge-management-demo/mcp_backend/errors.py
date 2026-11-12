"""Public errors contain explicit, safe messages, never exception strings."""
from dataclasses import dataclass, field


@dataclass
class KnowledgeError(Exception):
    code: str
    message: str
    status: int = 400
    retryable: bool = False
    details: dict = field(default_factory=dict)

    def public(self):
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "details": self.details}


def require(condition, code, message, status=400, **details):
    if not condition:
        raise KnowledgeError(code, message, status, details=details)
