class ApiError(Exception):
    """An error that maps directly to an HTTP status and a JSON {"error": message} body."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message
