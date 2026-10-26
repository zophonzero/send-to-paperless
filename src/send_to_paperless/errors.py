class AppError(Exception):
    """An error safe to show to the user (never include credentials)."""


class APIError(AppError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain
