from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime


class SupportTicketCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    subject: str = Field(..., min_length=1, max_length=255)
    category: str = Field(..., min_length=1, max_length=100)
    related_to: Optional[str] = Field(None, max_length=255)
    message: str = Field(..., min_length=10, max_length=1000)


class SupportTicketResponse(BaseModel):
    id: str
    user_id: Optional[str] = None
    name: str
    email: EmailStr
    subject: str
    category: str
    related_to: Optional[str] = None
    message: str
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
