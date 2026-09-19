import asyncio
from app.db.database import get_session_factory
from app.db.models import User
from sqlalchemy import select

async def main():
    async_session = get_session_factory()
    async with async_session() as session:
        result = await session.execute(select(User).where(User.email == 'rahul259006@gmail.com'))
        user = result.scalar_one_or_none()
        if user:
            print(f'User: {user.email}, Role: {user.role}, Status: {user.status}')
        else:
            print('User rahul259006@gmail.com not found')
        
        result2 = await session.execute(select(User).where(User.role == 'ADMIN'))
        admins = result2.scalars().all()
        if admins:
            for admin in admins:
                print(f'Admin: {admin.email}, Role: {admin.role}')
        else:
            print('No ADMIN users found')

if __name__ == '__main__':
    asyncio.run(main())
