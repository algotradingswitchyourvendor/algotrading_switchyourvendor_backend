"""
Upstox Equity Variables Market Data Scheduler.

This integrates the new `upstox_variables_v1.py` processing engine into
the existing MarketPulse `UpstoxScheduler` architecture.

Key features maintained from the original architecture:
- Environment variables via Settings
- Callback registration (for LiveCache)
- Single-file daily S3 appending (for History module compatibility)
- Fetch Timestamp (for History module timeline grouping)
- Holiday awareness
"""

import urllib.parse
import time
import os
import json
import logging
from typing import Optional, Callable
from io import BytesIO
from datetime import datetime, timezone

from zoneinfo import ZoneInfo
import pandas as pd
import requests
import pyarrow.parquet as pq
import boto3
from apscheduler.schedulers.background import BackgroundScheduler
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import pyotp

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class UpstoxScheduler:
    """
    Encapsulates the Upstox Variables data fetching pipeline.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.access_token: Optional[str] = None
        self.s3_client = boto3.client("s3")
        self.scheduler: Optional[BackgroundScheduler] = None
        self._on_data_callbacks: list[Callable] = []

        # Local cache files
        self.hist_cache_file = os.path.join(self.settings.CACHE_DIRECTORY, 'hist_minute_cache.json')
        self.modified_cache_file = os.path.join(self.settings.CACHE_DIRECTORY, 'modified_data_cache.json')
        self.local_variables_file = os.path.join(self.settings.CACHE_DIRECTORY, 'local_variables_cache.parquet')

        os.makedirs(self.settings.CACHE_DIRECTORY, exist_ok=True)

        # State dictionaries
        self.previous_volume_ = {}
        self.previous_last_price = {}
        self.previous_avg_price = {}
        self.premarket_data = {}
        self.premarket_traded_value = {}

        self._load_previous_volume()

        # Load ticker list and mappings from S3
        self.ticker_list, self.instrument_mapping, self.last_price_mapping = self._load_ticker_data()
        
        # Download variables file initially
        self._download_variables_file()

    def register_callback(self, callback: Callable[[pd.DataFrame], None]) -> None:
        """Register a callback invoked after each successful data fetch."""
        self._on_data_callbacks.append(callback)
        logger.info(f"Registered data callback: {callback.__qualname__}")

    # ── Initialization & Loading ────────────────────────────────────────

    def _load_previous_volume(self):
        """Load previous_volume_ from modified_data_cache.json."""
        if os.path.exists(self.modified_cache_file):
            try:
                with open(self.modified_cache_file, 'r') as f:
                    rows = json.load(f)
                self.previous_volume_.clear()
                self.previous_last_price.clear()
                self.previous_avg_price.clear()
                for row in rows:
                    ik = row.get('Instrument_key')
                    vol = row.get('Volume')
                    if ik and vol is not None:
                        self.previous_volume_[ik] = vol
                    lp = row.get('Last_Price')
                    if ik and lp is not None:
                        self.previous_last_price[ik] = lp
                    ap = row.get('Average_Price')
                    if ik and ap is not None:
                        self.previous_avg_price[ik] = ap
                logger.info(f"Loaded previous volume for {len(self.previous_volume_)} stocks.")
            except Exception as e:
                logger.warning(f"Could not load previous volume: {e}")

    def _load_ticker_data(self) -> tuple[list, dict, dict]:
        """Load instrument list and mappings from S3."""
        try:
            response = self.s3_client.get_object(
                Bucket=self.settings.S3_BUCKET_NAME,
                Key=self.settings.S3_TICKER_FILE_KEY,
            )
            ticker_df = pd.read_excel(BytesIO(response["Body"].read()))
            
            instrument_mapping = dict(zip(ticker_df['instrument_key'], ticker_df['instrument_key']))
            ticker_list = list(instrument_mapping.keys())
            last_price_mapping = dict(zip(ticker_df['instrument_key'], ticker_df['last_price']))
            
            logger.info(f"Successfully loaded {len(ticker_list)} tickers from S3")
            return ticker_list, instrument_mapping, last_price_mapping
        except Exception as e:
            logger.error(f"Error loading ticker data from S3: {e}")
            return [], {}, {}

    def _download_variables_file(self, force=False):
        """Download Variables file from S3 to local cache."""
        if force or not os.path.exists(self.local_variables_file):
            logger.info("Downloading Variables file from S3 to local disk...")
            try:
                self.s3_client.download_file(
                    self.settings.S3_BUCKET_NAME,
                    f'{self.settings.S3_VARIABLES_PREFIX}/Variables file.parquet',
                    self.local_variables_file
                )
                logger.info("Successfully downloaded Variables file.")
            except Exception as e:
                logger.error(f"Failed to download Variables file: {e}")
        else:
            logger.info("Variables file already exists on local disk.")

    def scheduled_download_variables(self):
        """Triggered daily at 08:58 to get fresh file."""
        logger.info("Fetching fresh Variables file for the day...")
        self._download_variables_file(force=True)

    def fetch_hist_from_local_disk(self, hour: int, minute: int) -> dict:
        """Process the local variables parquet file using PyArrow predicate pushdown."""
        import pyarrow.dataset as ds
        import gc
        try:
            if not os.path.exists(self.local_variables_file):
                return {}
                
            dataset = ds.dataset(self.local_variables_file, format="parquet")
            # Pushdown filter to parquet engine (avoids full table scan)
            table = dataset.to_table(filter=(ds.field('Hour') == hour) & (ds.field('Minute') == minute))
            df = table.to_pandas()
            
            if df.empty:
                del table, df
                return {}
                
            df['TradingSymbol'] = df['TradingSymbol'].astype(str).str.strip()
            df = df.drop_duplicates(subset=['TradingSymbol'])
            result = df.set_index('TradingSymbol').to_dict(orient='index')
            
            # Explicitly release large structures
            del table, df
            gc.collect()
            
            return result
        except Exception as e:
            logger.error(f"Local disk read failed: {e}")
            return {}

     # ── Authentication ──────────────────────────────────────────────────

    def auto_login(self) -> Optional[str]:
        """
        Authenticate with Upstox via Selenium headless browser.
        Returns the access token or None on failure.
        """
        try:
            options = Options()
            options.add_argument("--no-sandbox")
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-blink-features=AutomationControlled")

            driver = webdriver.Chrome(options=options)

            url = (
                f"https://api-v2.upstox.com/login/authorization/dialog"
                f"?response_type=code"
                f"&client_id={self.settings.UPSTOX_API_KEY}"
                f"&redirect_uri={urllib.parse.quote(self.settings.UPSTOX_REDIRECT_URI, safe='')}"
            )

            driver.get(url)

            wait = WebDriverWait(driver, 30)

            # Wait for React to render the mobile number input
            username_input = wait.until(EC.visibility_of_element_located((By.ID, "mobileNum")))
            username_input.clear()
            username_input.send_keys(self.settings.UPSTOX_CLIENT_ID)

            wait.until(EC.element_to_be_clickable((By.ID, "getOtp"))).click()

            # Wait for OTP input to become visible
            password_input = wait.until(EC.visibility_of_element_located((By.ID, "otpNum")))
            
            # Enter TOTP
            totp = pyotp.TOTP(self.settings.UPSTOX_TOTP_SECRET).now()
            password_input.clear()
            password_input.send_keys(totp)

            wait.until(EC.element_to_be_clickable((By.ID, "continueBtn"))).click()

            # Wait for PIN input to become visible
            pin_input = wait.until(EC.visibility_of_element_located((By.ID, "pinCode")))
            pin_input.clear()
            pin_input.send_keys(self.settings.UPSTOX_CLIENT_PIN)

            original_url = driver.current_url
            wait.until(EC.element_to_be_clickable((By.ID, "pinContinueBtn"))).click()

            # Wait until the URL changes from the login page
            wait.until(EC.url_changes(original_url))

            redirected_url = driver.current_url
            code = redirected_url.split("?code=")[1]

            # Exchange code for token
            token_url = "https://api.upstox.com/v2/login/authorization/token"
            headers = {
                "accept": "application/json",
                "Api-Version": "2.0",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            data = {
                "code": code,
                "client_id": self.settings.UPSTOX_API_KEY,
                "client_secret": self.settings.UPSTOX_SECRET_KEY,
                "redirect_uri": self.settings.UPSTOX_REDIRECT_URI,
                "grant_type": "authorization_code",
            }

            response = requests.post(token_url, headers=headers, data=data)
            json_response = response.json()
            access_token = json_response["access_token"]

            driver.quit()
            logger.info("Login successful")
            return str(access_token)

        except Exception as e:
            logger.error(f"Login failed: {e}")
            return None

    # ── Data Fetching ───────────────────────────────────────────────────

    @staticmethod
    def _epoch_to_ist(epoch_time) -> Optional[datetime]:
        try:
            epoch_str = str(epoch_time)
            if len(epoch_str) == 13:
                epoch_int = int(epoch_str[:-3])
            else:
                epoch_int = int(epoch_str)
            utc_time = datetime.fromtimestamp(epoch_int, tz=timezone.utc)
            return utc_time.astimezone(IST)
        except Exception:
            return None

    def fetch_fno_data(self, fnolist: list) -> pd.DataFrame:
        if not fnolist:
            return pd.DataFrame()

        try:
            instrument_keys = ",".join(map(urllib.parse.quote, fnolist))
            url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={instrument_keys}"
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access_token}",
            }

            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json().get("data", {})

            rows = []
            now = datetime.now(IST)
            for instrument, details in data.items():
                try:
                    row = {
                        "instrument_key": details.get("instrument_token"),
                        "Instrument": instrument,
                        "Symbol": details.get("symbol"),
                        "Hour": now.hour,
                        "Minute": now.minute,
                        "Open": (details.get("ohlc") or {}).get("open"),
                        "High": (details.get("ohlc") or {}).get("high"),
                        "Low": (details.get("ohlc") or {}).get("low"),
                        "Close": (details.get("ohlc") or {}).get("close"),
                        "Last Price": details.get("last_price"),
                        "Volume": details.get("volume"),
                        "Average Price": details.get("average_price"),
                        "Open Interest": details.get("oi"),
                        "Net Change": details.get("net_change"),
                        "Total Buy Quantity": details.get("total_buy_quantity"),
                        "Total Sell Quantity": details.get("total_sell_quantity"),
                        "Lower Circuit Limit": details.get("lower_circuit_limit"),
                        "Upper Circuit Limit": details.get("upper_circuit_limit"),
                        "Last Trade Time": (
                            self._epoch_to_ist(details.get("last_trade_time"))
                            if details.get("last_trade_time") else None
                        ),
                        "OI Day High": details.get("oi_day_high"),
                        "OI Day Low": details.get("oi_day_low"),
                        # Explicitly maintained for History/Timeline compatibility
                        "Fetch Timestamp": now,
                    }
                    rows.append(row)
                except Exception:
                    pass

            try:
                # Wrap pd.DataFrame to isolate if this is what crashes
                result_df = pd.DataFrame(rows)
                return result_df
            except Exception as df_err:
                logger.error(f"pd.DataFrame(rows) crashed! Length of rows: {len(rows)}")
                if len(rows) > 0:
                    logger.error(f"First row: {rows[0]}")
                    logger.error(f"Types in first row: {[(k, type(v)) for k, v in rows[0].items()]}")
                raise df_err

        except Exception as e:
            logger.error("=== RUNTIME BUG INVESTIGATION (fetch_fno_data) ===")
            logger.error(f"Local variables at exception time:")
            for k, v in locals().items():
                if k not in ["self", "data", "rows"]:
                    logger.error(f"{k}: {type(v)} = {v}")
            logger.exception("API request error in fetch_fno_data:")
            return pd.DataFrame()

    def fetch_all_fno_data(self) -> pd.DataFrame:
        all_data = []
        chunk_size = self.settings.FETCH_CHUNK_SIZE
        total_tickers = len(self.ticker_list)

        for i in range(0, total_tickers, chunk_size):
            chunk = self.ticker_list[i : i + chunk_size]
            end_idx = min(i + chunk_size, total_tickers)
            logger.info(f"Fetching tickers {i + 1} to {end_idx}...")
            
            df = self.fetch_fno_data(chunk)

            if not df.empty:
                all_data.append(df)

        final_df = pd.concat(all_data, ignore_index=True) if all_data else pd.DataFrame()
        if not final_df.empty:
            logger.info(f"Fetched data:\n{final_df.shape[0]} rows × {final_df.shape[1]} cols")
        return final_df

    # ── Processing & Enrichment ─────────────────────────────────────────

    def check_premarket_gain(self, live_df: pd.DataFrame):
        """Calculates premarket gain at 09:15 for all stocks."""
        logger.info("Running Pre-Market Gain calculation...")
        count = 0
        for _, row in live_df.iterrows():
            instrument_key = row.get('instrument_key')
            current_open = pd.to_numeric(row.get('Open'), errors='coerce')
            prev_close = self.last_price_mapping.get(instrument_key)
            
            gain_pct = None
            if pd.notna(prev_close) and pd.notna(current_open) and prev_close > 0:
                gain_pct = round(((current_open - prev_close) / prev_close) * 100, 2)
            
            self.premarket_data[instrument_key] = {
                "Previous_Day_Close": prev_close,
                "Current_Open": current_open,
                "Pre_Market_Gain_Pct": gain_pct,
            }
            count += 1
        logger.info(f"Pre-Market Gain calculated for {count} stocks.")

    def capture_premarket_traded_value(self):
        """Captures Volume * Close Price at 09:10."""
        try:
            logger.info("Capturing 9:10 AM Pre-Market Traded Value...")
            final_df = self.fetch_all_fno_data()
            if not final_df.empty:
                count = 0
                for _, row in final_df.iterrows():
                    ik = row.get('instrument_key')
                    volume = pd.to_numeric(row.get('Volume'), errors='coerce')
                    close = pd.to_numeric(row.get('Close'), errors='coerce')
                    if ik and pd.notna(volume) and pd.notna(close):
                        self.premarket_traded_value[ik] = round((volume * close) / 10000000, 4)
                        count += 1
                logger.info(f"Pre-Market Traded Value captured for {count} stocks.")
        except Exception as e:
            logger.error(f"Error capturing premarket traded value: {e}")

    def prefetch_next_minute(self, current_hour: int, current_minute: int):
        next_minute = current_minute + 1
        next_hour = current_hour
        if next_minute >= 60:
            next_minute = 0
            next_hour += 1
        
        if next_hour > 15 or (next_hour == 15 and next_minute > 30):
            return
            
        hist_dict = self.fetch_hist_from_local_disk(next_hour, next_minute)
        cache_data = {"hour": next_hour, "minute": next_minute, "data": hist_dict}
        try:
            with open(self.hist_cache_file, 'w') as f:
                json.dump(cache_data, f, default=str)
        except Exception as e:
            logger.error(f"Failed to save hist cache: {e}")

    def load_hist_cache(self, current_hour: int, current_minute: int) -> dict:
        if os.path.exists(self.hist_cache_file):
            try:
                with open(self.hist_cache_file, 'r') as f:
                    cache = json.load(f)
                if cache.get('hour') == current_hour and cache.get('minute') == current_minute:
                    return cache['data']
            except Exception:
                pass
        return self.fetch_hist_from_local_disk(current_hour, current_minute)

    def save_modified_cache(self, output_rows: list):
        try:
            with open(self.modified_cache_file, 'w') as f:
                json.dump(output_rows, f, default=str)
        except Exception as e:
            logger.error(f"Failed to save modified cache: {e}")

    def process_data(self, live_df: pd.DataFrame, hist_time_dict: dict) -> pd.DataFrame:
        """Merges live data with historical benchmarks and calculates metrics."""
        if live_df.empty:
            return pd.DataFrame()

        now = datetime.now(IST)
        current_hour, current_minute, current_second = now.hour, now.minute, now.second
        
        output_rows = []
        for _, row in live_df.iterrows():
            instrument_key = row.get('instrument_key')
            symbol = row.get('Symbol')
            current_volume = pd.to_numeric(row.get('Volume'), errors='coerce')
            
            if pd.isna(current_volume) or not instrument_key or not symbol:
                continue
            
            clean_symbol = str(symbol).split(':')[-1] if ':' in str(symbol) else symbol
            trading_symbol = clean_symbol  # e.g. "INFY" — used by frontend search, pinning, rendering
            exchange = str(symbol).split('_')[0] if '_' in str(symbol) else "NSE"  # e.g. "NSE"
            
            delta_volume_ = 0
            if instrument_key in self.previous_volume_:
                delta_volume_ = current_volume - self.previous_volume_[instrument_key]
            
            last_price = pd.to_numeric(row.get('Last Price'), errors='coerce')
            open_price = pd.to_numeric(row.get('Open'), errors='coerce')
            close_price = pd.to_numeric(row.get('Close'), errors='coerce')
            avg_price = pd.to_numeric(row.get('Average Price'), errors='coerce')
            
            traded_value_ = delta_volume_ * last_price if pd.notna(last_price) and pd.notna(delta_volume_) else 0
            
            hist = hist_time_dict.get(clean_symbol, {})
            
            live_tbq = pd.to_numeric(row.get('Total Buy Quantity'), errors='coerce')
            live_tsq = pd.to_numeric(row.get('Total Sell Quantity'), errors='coerce')
            tbq_30d = pd.to_numeric(hist.get('TBQ_30D_Avg'), errors='coerce') if hist.get('TBQ_30D_Avg') is not None else None
            tsq_30d = pd.to_numeric(hist.get('TSQ_30D_Avg'), errors='coerce') if hist.get('TSQ_30D_Avg') is not None else None
            
            tbq_vs_30d = (live_tbq / tbq_30d) if pd.notna(live_tbq) and pd.notna(tbq_30d) and tbq_30d != 0 else None
            tsq_vs_30d = (live_tsq / tsq_30d) if pd.notna(live_tsq) and pd.notna(tsq_30d) and tsq_30d != 0 else None
            
            prev_lp = self.previous_last_price.get(instrument_key)
            mom_gain, mom_gain_pct = None, None
            if prev_lp is not None and pd.notna(last_price):
                prev_lp = pd.to_numeric(prev_lp, errors='coerce')
                if pd.notna(prev_lp) and prev_lp != 0:
                    mom_gain = last_price - prev_lp
                    mom_gain_pct = round((mom_gain / prev_lp) * 100, 4)
            
            delta_day_gain_pct = None
            if pd.notna(last_price) and pd.notna(open_price) and open_price != 0:
                delta_day_gain = last_price - open_price
                delta_day_gain_pct = round((delta_day_gain / open_price) * 100, 4)
            
            actual_day_gain_pct = None
            net_change = pd.to_numeric(row.get('Net Change'), errors='coerce')
            if pd.notna(last_price) and pd.notna(net_change):
                prev_close_computed = last_price - net_change
                if prev_close_computed != 0:
                    actual_day_gain_pct = round((net_change / prev_close_computed) * 100, 4)
            
            delta_avg_price = None
            prev_ap = self.previous_avg_price.get(instrument_key)
            prev_vol = self.previous_volume_.get(instrument_key)
            if (pd.notna(avg_price) and prev_ap is not None and prev_vol is not None and delta_volume_ != 0):
                prev_ap = pd.to_numeric(prev_ap, errors='coerce')
                prev_vol = pd.to_numeric(prev_vol, errors='coerce')
                if pd.notna(prev_ap) and pd.notna(prev_vol):
                    delta_avg_price = round(
                        (current_volume * avg_price - prev_vol * prev_ap) / delta_volume_, 2
                    )
            
            w52_high = pd.to_numeric(hist.get('Week_52_High'), errors='coerce') if hist.get('Week_52_High') is not None else None
            w52_low = pd.to_numeric(hist.get('Week_52_Low'), errors='coerce') if hist.get('Week_52_Low') is not None else None
            
            move_from_52w_high_pct = None
            if pd.notna(last_price) and pd.notna(w52_high) and w52_high != 0:
                move_from_52w_high_pct = round(((last_price - w52_high) / w52_high) * 100, 2)
            
            move_from_52w_low_pct = None
            if pd.notna(last_price) and pd.notna(w52_low) and w52_low != 0:
                move_from_52w_low_pct = round(((last_price - w52_low) / w52_low) * 100, 2)
            
            # Update caches for next tick
            self.previous_volume_[instrument_key] = current_volume
            if pd.notna(last_price):
                self.previous_last_price[instrument_key] = last_price
            if pd.notna(avg_price):
                self.previous_avg_price[instrument_key] = avg_price
            
            pm = self.premarket_data.get(instrument_key, {})
            pm_tv = self.premarket_traded_value.get(instrument_key)
            
            output_rows.append({
                "Symbol": symbol,
                "Instrument": row.get('Instrument'),
                "instrument_key": instrument_key,
                "trading_symbol": trading_symbol,
                "exchange": exchange,
                "Hour": current_hour,
                "Minute": current_minute,
                "Second": current_second,
                "Open": open_price,
                "High": pd.to_numeric(row.get('High'), errors='coerce'),
                "Low": pd.to_numeric(row.get('Low'), errors='coerce'),
                "Last Price": last_price,
                "Prev_Day_Close": close_price,
                "Volume": current_volume,
                "Average Price": avg_price,
                "Net Change": pd.to_numeric(row.get('Net Change'), errors='coerce'),
                "Total Buy Quantity": live_tbq,
                "Total Sell Quantity": live_tsq,
                "Upper Circuit Limit": pd.to_numeric(row.get('Upper Circuit Limit'), errors='coerce'),
                "Lower Circuit Limit": pd.to_numeric(row.get('Lower Circuit Limit'), errors='coerce'),
                "Last Trade Time": row.get('Last Trade Time'),
                "Fetch Timestamp": row.get('Fetch Timestamp'),  # ESSENTIAL FOR HISTORY TIMELINE
                "Calculated_Delta_Volume": delta_volume_,
                "Calculated_Traded_Value": traded_value_,
                "Calculated_TBQ_vs_30D": tbq_vs_30d,
                "Calculated_TSQ_vs_30D": tsq_vs_30d,
                "Delta_Day_Gain_Pct": delta_day_gain_pct,
                "Actual_Day_Gain_Pct": actual_day_gain_pct,
                "day_change_pct": actual_day_gain_pct,  # Map to existing column expectation
                "MoM_Gain_Pct": mom_gain_pct,
                "Delta_Average_Price": delta_avg_price,
                "Movement_From_52W_High_Pct": move_from_52w_high_pct,
                "Movement_From_52W_Low_Pct": move_from_52w_low_pct,
                "TBQ_3D_Avg": hist.get('TBQ_3D_Avg'),
                "TBQ_7D_Avg": hist.get('TBQ_7D_Avg'),
                "TBQ_15D_Avg": hist.get('TBQ_15D_Avg'),
                "TBQ_30D_Avg": tbq_30d,
                "TSQ_3D_Avg": hist.get('TSQ_3D_Avg'),
                "TSQ_7D_Avg": hist.get('TSQ_7D_Avg'),
                "TSQ_15D_Avg": hist.get('TSQ_15D_Avg'),
                "TSQ_30D_Avg": tsq_30d,
                "VOL_3D_Avg": hist.get('VOL_3D_Avg'),
                "VOL_7D_Avg": hist.get('VOL_7D_Avg'),
                "VOL_15D_Avg": hist.get('VOL_15D_Avg'),
                "VOL_30D_Avg": hist.get('VOL_30D_Avg'),
                "VOL_Prev_Day": hist.get('VOL_Prev_Day'),
                "DLV_7D_Avg": hist.get('DLV_7D_Avg'),
                "DLV_15D_Avg": hist.get('DLV_15D_Avg'),
                "DLV_30D_Avg": hist.get('DLV_30D_Avg'),
                "DLV_Prev_Day": hist.get('DLV_Prev_Day'),
                "TBQ_Prev_Day": hist.get('TBQ_Prev_Day'),
                "TSQ_Prev_Day": hist.get('TSQ_Prev_Day'),
                "Prev_Daily_Gain": hist.get('Prev_Daily_Gain'),
                "Prev_Premarket_Gain": hist.get('Prev_Pre_market_Gain'),
                "Prev_Day_Spread": hist.get('Prev_Day_Spread'),
                "Prev_Day_Upward_Movement": hist.get('Prev_Day_Upward_Movement'),
                "Prev_Day_Downward_Movement": hist.get('Prev_Day_Downward_Movement'),
                "Premarket_Prev_Day_Close": pm.get('Previous_Day_Close'),
                "Premarket_Current_Open": pm.get('Current_Open'),
                "Premarket_Gain_Pct": pm.get('Pre_Market_Gain_Pct'),
                "Premarket_Traded_Value_Cr": pm_tv,
            })
            
        if output_rows:
            self.save_modified_cache(output_rows)
            return pd.DataFrame(output_rows)
        return pd.DataFrame()

    # ── S3 Uploading ────────────────────────────────────────────────────

    def _generate_daily_filename(self) -> str:
        current_date = datetime.now(IST).strftime("%Y-%m-%d")
        return f"{self.settings.S3_PARQUET_PREFIX}/{current_date}_Equity.parquet"

    def upload_to_s3(self, df: pd.DataFrame) -> None:
        """Uploads to S3 by appending to today's parquet file (Preserves History compatibility)."""
        import time
        import gc
        import pyarrow as pa
        import pyarrow.parquet as pq
        try:
            file_name = self._generate_daily_filename()
            try:
                t0 = time.time()
                parquet_obj = self.s3_client.get_object(
                    Bucket=self.settings.S3_BUCKET_NAME, Key=file_name
                )
                body_bytes = parquet_obj["Body"].read()
                t_download = time.time() - t0
                
                t0 = time.time()
                existing_table = pq.read_table(BytesIO(body_bytes))
                t_read = time.time() - t0
                
                # Free the large bytes string immediately
                del body_bytes
                gc.collect()
            except self.s3_client.exceptions.NoSuchKey:
                existing_table = None
                t_download, t_read = 0.0, 0.0
            except Exception as e:
                logger.warning(f"Could not read existing parquet: {e}")
                existing_table = None
                t_download, t_read = 0.0, 0.0

            int_cols = ["Hour", "Minute", "Second"]
            
            for col in int_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")

            t0 = time.time()
            if existing_table is not None:
                # Convert new data to PyArrow matching the existing schema
                try:
                    new_table = pa.Table.from_pandas(df, schema=existing_table.schema)
                except Exception:
                    new_table = pa.Table.from_pandas(df)
                    
                # Zero-copy concatenation (just links chunk pointers in memory)
                combined_table = pa.concat_tables([existing_table, new_table], promote_options='default')
                
                # Release existing tables
                existing_mem = existing_table.nbytes / (1024**2)
                del existing_table, new_table
                gc.collect()
            else:
                combined_table = pa.Table.from_pandas(df)
                existing_mem = 0.0
            t_concat = time.time() - t0
            
            logger.info("=== S3 MEMORY PROFILING (PYARROW) ===")
            logger.info(f"existing_table memory: {existing_mem:.2f} MB")
            logger.info(f"new_df pandas memory: {df.memory_usage(deep=True).sum() / (1024**2):.2f} MB")
            logger.info(f"combined_table memory: {combined_table.nbytes / (1024**2):.2f} MB")
            logger.info("=== S3 TIMING PROFILING ===")
            logger.info(f"S3 download: {t_download:.2f} sec")
            logger.info(f"pq.read_table(): {t_read:.2f} sec")
            logger.info(f"pa.concat_tables(): {t_concat:.2f} sec")

            parquet_buffer = BytesIO()
            pq.write_table(combined_table, parquet_buffer)
            
            # Release massive combined table before S3 upload
            del combined_table
            gc.collect()

            t0 = time.time()
            self.s3_client.put_object(
                Bucket=self.settings.S3_BUCKET_NAME,
                Key=file_name,
                Body=parquet_buffer.getvalue(),
            )
            t_upload = time.time() - t0
            logger.info(f"upload: {t_upload:.2f} sec")
            logger.info(f"S3 upload complete: s3://{self.settings.S3_BUCKET_NAME}/{file_name}")
            
            # Final cleanup
            del parquet_buffer
            gc.collect()
        except Exception as e:
            logger.error(f"Error uploading to S3: {e}")

    # ── Scheduled Tasks ─────────────────────────────────────────────────

    def _premarket_task(self):
        from app.config.holidays import is_market_open
        if not is_market_open():
            return
        logger.info("Starting Pre-Market Gain Task...")
        try:
            final_df = self.fetch_all_fno_data()
            if not final_df.empty:
                self.check_premarket_gain(final_df)
        except Exception as e:
            logger.error(f"Error in premarket_task: {e}")

    def _capture_premarket_traded_value_task(self):
        from app.config.holidays import is_market_open
        if not is_market_open():
            return
        self.capture_premarket_traded_value()

    def _fetch_and_publish(self):
        from app.config.holidays import is_market_open
        if not is_market_open():
            return
            
        if not self.access_token:
            logger.warning("No access token, skipping fetch")
            return

        try:
            import time
            total_start = time.time()
            
            logger.info("Starting data fetch cycle...")
            
            now = datetime.now(IST)
            current_hour, current_minute = now.hour, now.minute
            
            t0 = time.time()
            hist_time_dict = self.load_hist_cache(current_hour, current_minute)
            t_hist = time.time() - t0
            
            t0 = time.time()
            raw_df = self.fetch_all_fno_data()
            t_fetch = time.time() - t0
            
            if raw_df.empty:
                return

            t0 = time.time()
            master_df = self.process_data(raw_df, hist_time_dict)
            t_process = time.time() - t0
            
            if master_df.empty:
                return

            t0 = time.time()
            # Append to single daily Parquet
            self.upload_to_s3(master_df)
            t_s3 = time.time() - t0

            # Update cache/dashboard immediately
            t_cache = 0.0
            t_cb = 0.0
            for callback in self._on_data_callbacks:
                try:
                    res = callback(master_df)
                    if isinstance(res, tuple) and len(res) == 2:
                        t_cache += res[0]
                        t_cb += res[1]
                except Exception as e:
                    logger.error(f"Callback failed: {e}")

            # Prefetch for next minute
            t0 = time.time()
            self.prefetch_next_minute(current_hour, current_minute)
            t_prefetch = time.time() - t0
            
            total_end = time.time()
            total_cycle = total_end - total_start
            
            summary = (
                f"\n{'Loading History Cache ':.<31} {t_hist:.2f} sec\n"
                f"{'Fetching Market Data ':.<31} {t_fetch:.2f} sec\n"
                f"{'Processing Data ':.<31} {t_process:.2f} sec\n"
                f"{'Uploading to S3 ':.<31} {t_s3:.2f} sec\n"
                f"{'Updating LiveCache ':.<31} {t_cache:.2f} sec\n"
                f"{'Running Callbacks ':.<31} {t_cb:.2f} sec\n"
                f"{'Prefetch Next Minute ':.<31} {t_prefetch:.2f} sec\n\n"
                f"{'TOTAL FETCH CYCLE ':.<31} {total_cycle:.2f} sec\n"
            )
            logger.info(summary)
            
            logger.info("Fetch cycle complete")
            
        except Exception as e:
            logger.error(f"Error in fetch cycle: {e}")

    def _refresh_token(self):
        self.access_token = self.auto_login()

    def start(self):
        self.access_token = self.auto_login()
        self.scheduler = BackgroundScheduler()

        # Core market hours tick
        self.scheduler.add_job(
            self._fetch_and_publish, "cron", day_of_week="mon-fri", hour="9", minute="15-59"
        )
        self.scheduler.add_job(
            self._fetch_and_publish, "cron", day_of_week="mon-fri", hour="10-14", minute="*"
        )
        self.scheduler.add_job(
            self._fetch_and_publish, "cron", day_of_week="mon-fri", hour="15", minute="0-30"
        )

        # Premarket setup jobs
        self.scheduler.add_job(
            self._capture_premarket_traded_value_task, "cron", day_of_week="mon-fri", hour="9", minute="10"
        )
        self.scheduler.add_job(
            self._premarket_task, "cron", day_of_week="mon-fri", hour="9", minute="15"
        )
        
        # Token and static data refresh
        self.scheduler.add_job(
            self._refresh_token, "cron", day_of_week="mon-fri", hour="8", minute="55"
        )
        self.scheduler.add_job(
            self.scheduled_download_variables, "cron", day_of_week="mon-fri", hour="8", minute="58"
        )

        self.scheduler.start()
        logger.info("Upstox Variables Scheduler started")

    def stop(self):
        if self.scheduler:
            self.scheduler.shutdown(wait=False)

    @property
    def is_running(self):
        return self.scheduler is not None and self.scheduler.running
