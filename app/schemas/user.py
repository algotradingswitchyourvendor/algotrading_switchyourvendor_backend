from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List
from datetime import datetime

class ProfileUpdate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)

class UserPreferencesResponse(BaseModel):
    default_exchange: str
    default_page_size: int
    timezone: str

class UserPreferencesUpdate(BaseModel):
    default_exchange: Optional[str] = None
    default_page_size: Optional[int] = None
    timezone: Optional[str] = None

class SessionResponse(BaseModel):
    id: str
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: datetime
    expires_at: datetime
    is_current: bool

class ConnectionResponse(BaseModel):
    provider: str
    provider_account_id: str
    created_at: datetime

class BillingHistoryResponse(BaseModel):
    id: str
    amount_inr: int
    currency: str
    status: str
    razorpay_payment_id: Optional[str]
    created_at: datetime
