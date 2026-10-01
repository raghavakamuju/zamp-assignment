from pydantic import BaseModel, EmailStr


class NewRunRequest(BaseModel):
    prospect_name: str
    company_name: str
    title: str | None = None


class AuthRequest(BaseModel):
    email: EmailStr
    password: str
