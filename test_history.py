import asyncio
from app.services.history_service import get_historical_data

async def test():
    records, meta = await get_historical_data(target_date='2026-07-02')
    print("KEYS:", list(records[0].keys()))
    print("RECORD 0:", records[0])

if __name__ == "__main__":
    asyncio.run(test())
