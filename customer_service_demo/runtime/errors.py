class ServiceFailure(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.public_message, self.retryable = code, message, retryable


class InvalidModelOutput(ValueError):
    pass
