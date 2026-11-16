from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

class DomainError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        self.code, self.message = code, message
        self.status_code = status_code
        super().__init__(message)

def install(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status_code,
                            content={"error": exc.code, "code": exc.code, "message": exc.message})
