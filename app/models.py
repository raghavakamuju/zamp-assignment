from pydantic import BaseModel, EmailStr


class NewRunRequest(BaseModel):
    prospect_name: str
    prospect_email: EmailStr | None = None
    company_name: str
    title: str | None = None


class AuthRequest(BaseModel):
    email: EmailStr
    password: str


class RejectRequest(BaseModel):
    reason: str
