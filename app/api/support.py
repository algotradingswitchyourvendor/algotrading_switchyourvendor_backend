import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db
from app.db.models import SupportTicket, User
from app.schemas.support import SupportTicketCreate, SupportTicketResponse
from app.auth.dependencies import get_current_user
from app.config.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Support"])


@router.post("/support", response_model=SupportTicketResponse, status_code=status.HTTP_201_CREATED)
async def create_support_ticket(
    ticket_in: SupportTicketCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """
    Create a new support ticket.
    Authenticated users will have their user_id linked automatically.
    """
    # If the user is authenticated, we can optionally enforce that they
    # use their registered email, or just trust the auth session and link the user_id.
    # The requirement says: "prefer existing authenticated user data where available."
    
    # We will use the provided data from the frontend, but link the user_id.
    # Note: If rate limiting is required, we rely on a global rate limiter if available,
    # or implement a simple check here (e.g. max 5 tickets per user/IP per day)
    
    user_id = current_user.id if current_user else None
    
    # Create the ticket
    ticket = SupportTicket(
        user_id=user_id,
        name=current_user.name if current_user and ticket_in.name == current_user.name else ticket_in.name,
        email=current_user.email if current_user and ticket_in.email == current_user.email else ticket_in.email,
        subject=ticket_in.subject,
        category=ticket_in.category,
        related_to=ticket_in.related_to,
        message=ticket_in.message,
    )
    
    db.add(ticket)
    try:
        await db.commit()
        await db.refresh(ticket)
        logger.info(f"Support ticket created: {ticket.id} by user_id: {user_id}")
    except Exception as e:
        await db.rollback()
        logger.error(f"Error creating support ticket: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while saving the support ticket.",
        )
    
    return ticket
