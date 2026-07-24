import asyncio
from app.services.history.history_service import HistoryService
async def main():
    s = HistoryService()
    res, meta = await s.get_historical_data(page=1, page_size=10)
    print('Historical Data length:', len(res))
    res2 = await s.get_stock_timeline('INFY', '2026-07-24')
    print('Timeline length:', len(res2))
asyncio.run(main())
