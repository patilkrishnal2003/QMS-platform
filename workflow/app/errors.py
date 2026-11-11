class WorkflowError(Exception):
    """A refused action. `status` is the HTTP status; `required` lists what a human must still do."""

    def __init__(self, message: str, status: int = 409, required: list | None = None, code: str = "refused"):
        super().__init__(message)
        self.status, self.required, self.code = status, required or [], code

    def as_dict(self) -> dict:
        return {"detail": str(self), "code": self.code, "required": self.required}
