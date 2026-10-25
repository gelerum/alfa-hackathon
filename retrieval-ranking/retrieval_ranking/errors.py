"""Stable public error codes; database credentials never enter HTTP responses."""


class RankingError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ContextNotReady(RankingError):
    def __init__(self, message: str = "No compatible ready company context is available"):
        super().__init__("context_not_ready", message)


class BusyError(RankingError):
    def __init__(self):
        super().__init__("ranker_busy", "The CPU ranker is processing another request", 429)
