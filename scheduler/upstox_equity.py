"""
Upstox Equity Market Data Scheduler.

This is the EXISTING backend scheduler, minimally modified to:
1. Read credentials from environment variables instead of hardcoded values
2. Fix the instrument_key mapping bug
3. Add enrichment columns (trading_symbol, exchange, day_change_pct)
4. Add a callback hook to update LiveCache after each fetch cycle
5. Add holiday awareness to skip weekends/holidays
6. Improve error logging (remove silent except:pass)

The core fetch logic (auto_login, fetch_fno_data, fetch_all_fno_data,
upload_custom_data_to_s3) remains functionally identical.
"""

import urllib.parse
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import pyotp
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import urllib
from zoneinfo import ZoneInfo
import pandas as pd
from io import BytesIO
from datetime import datetime, timezone
import requests
from apscheduler.schedulers.background import BackgroundScheduler
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class UpstoxScheduler:
    """
    Encapsulates the Upstox data fetching pipeline.

    Previously this was all module-level globals. Wrapping in a class allows:
    - Dependency injection of settings
    - Clean lifecycle management (start/stop)
    - Callback registration for LiveCache updates
    - Testability
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.access_token: Optional[str] = None
        self.s3_client = boto3.client("s3")
        self.scheduler: Optional[BackgroundScheduler] = None
        self._on_data_callbacks: list[Callable] = []

        # Load ticker list and mappings from S3
        self.ticker_list, self.instrument_mapping, self.ticker_metadata = (
            self._load_ticker_data()
        )
        logger.info(f"Loaded {len(self.ticker_list)} tickers from S3")

    def register_callback(self, callback: Callable[[pd.DataFrame], None]) -> None:
        """
        Register a callback invoked after each successful data fetch.
        
        This is the integration point with LiveCache:
            scheduler.register_callback(live_cache.update)
        """
        self._on_data_callbacks.append(callback)
        logger.info(f"Registered data callback: {callback.__qualname__}")

    # ── Ticker Data Loading ─────────────────────────────────────────────

    def _load_ticker_data(self) -> tuple[list, dict, pd.DataFrame]:
        """
        Load instrument list and metadata from S3 Excel file.
        
        Returns:
            - ticker_list: list of instrument keys
            - instrument_mapping: dict mapping API response keys to instrument keys
            - ticker_metadata: DataFrame with company names, sectors, etc.
        """
        try:
            response = self.s3_client.get_object(
                Bucket=self.settings.S3_BUCKET_NAME,
                Key=self.settings.S3_TICKER_FILE_KEY,
            )
            ticker_df = pd.read_excel(BytesIO(response["Body"].read()))

            ticker_list = ticker_df["instrument_key"].tolist()

            # FIX: Build mapping that handles the API response format.
            # Upstox API returns keys like "NSE_EQ:INFY" in the response,
            # which matches the instrument_key format in the Excel file.
            instrument_mapping = dict(
                zip(ticker_df["instrument_key"], ticker_df["instrument_key"])
            )

            logger.info(
                f"Successfully loaded {len(ticker_list)} tickers from S3"
            )
            return ticker_list, instrument_mapping, ticker_df

        except Exception as e:
            logger.error(f"Error loading ticker data from S3: {e}")
            return [], {}, pd.DataFrame()

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
        """Convert epoch timestamp to IST datetime."""
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

    def _fetch_chunk(self, chunk: list) -> pd.DataFrame:
        """Fetch market data for a chunk of instruments (max 490)."""
        if not chunk:
            return pd.DataFrame()

        try:
            instrument_keys = ",".join(map(urllib.parse.quote, chunk))
            url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={instrument_keys}"
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access_token}",
            }

            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json().get("data", {})

            rows = []
            for instrument, details in data.items():
                try:
                    row = {
                        "instrument_key": self.instrument_mapping.get(instrument, instrument),
                        "Instrument": instrument,
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
                            if details.get("last_trade_time")
                            else None
                        ),
                        "OI Day High": details.get("oi_day_high"),
                        "OI Day Low": details.get("oi_day_low"),
                        "Fetch Timestamp": datetime.now(IST),
                    }
                    rows.append(row)
                except Exception as e:
                    logger.warning(f"Error parsing instrument {instrument}: {e}")

            return pd.DataFrame(rows)

        except requests.exceptions.RequestException as e:
            logger.error(f"API request error: {e}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Unexpected error in _fetch_chunk: {e}")
            return pd.DataFrame()

    def fetch_all_data(self) -> pd.DataFrame:
        """Fetch data for all instruments in chunks."""
        all_data = []
        chunk_size = self.settings.FETCH_CHUNK_SIZE

        for i in range(0, len(self.ticker_list), chunk_size):
            chunk = self.ticker_list[i : i + chunk_size]
            logger.info(f"Fetching tickers {i + 1} to {i + len(chunk)}...")

            df = self._fetch_chunk(chunk)
            if not df.empty:
                all_data.append(df)
            else:
                logger.warning(f"No data for chunk {i + 1} to {i + len(chunk)}")

        if not all_data:
            return pd.DataFrame()

        combined = pd.concat(all_data, ignore_index=True)
        return self._enrich_dataframe(combined)

    def _enrich_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add derived columns to the raw DataFrame.
        
        These are lightweight enrichments, not heavy calculations.
        Future calculation engine metrics will be added here.
        """
        if df.empty:
            return df

        # Extract trading symbol: "NSE_EQ:INFY" → "INFY"
        if "Instrument" in df.columns:
            df["trading_symbol"] = df["Instrument"].apply(
                lambda x: x.split(":")[-1] if isinstance(x, str) and ":" in x else x
            )
            df["exchange"] = df["Instrument"].apply(
                lambda x: x.split("_")[0] if isinstance(x, str) and "_" in x else ""
            )

        # Day change percentage
        if "Net Change" in df.columns and "Last Price" in df.columns:
            prev_close = df["Last Price"] - df["Net Change"]
            df["day_change_pct"] = (
                (df["Net Change"] / prev_close * 100)
                .round(2)
                .replace([float("inf"), float("-inf")], 0)
                .fillna(0)
            )

        # Enrich with company metadata from ticker Excel if available
        if not self.ticker_metadata.empty and "instrument_key" in df.columns:
            metadata_cols = [
                col for col in self.ticker_metadata.columns
                if col not in df.columns and col != "instrument_key"
            ]
            if metadata_cols:
                merge_df = self.ticker_metadata[["instrument_key"] + metadata_cols]
                df = df.merge(merge_df, on="instrument_key", how="left")

        return df

    # ── S3 Upload ───────────────────────────────────────────────────────

    def _generate_daily_filename(self) -> str:
        """Generate S3 key for today's parquet file."""
        current_date = datetime.now(IST).strftime("%Y-%m-%d")
        return f"{self.settings.S3_PARQUET_PREFIX}/{current_date}_Equity.parquet"

    def upload_to_s3(self, df: pd.DataFrame) -> None:
        """Upload DataFrame to S3 by appending to today's parquet file."""
        import pyarrow.parquet as pq
        import pyarrow as pa
        try:
            file_name = self._generate_daily_filename()

            # Read existing data if present
            try:
                parquet_obj = self.s3_client.get_object(
                    Bucket=self.settings.S3_BUCKET_NAME, Key=file_name
                )
                existing_table = pq.read_table(BytesIO(parquet_obj["Body"].read()))
            except self.s3_client.exceptions.NoSuchKey:
                existing_table = None
            except Exception as e:
                logger.warning(f"Could not read existing parquet, starting fresh: {e}")
                existing_table = None

            try:
                new_table = pa.Table.from_pandas(df, schema=existing_table.schema if existing_table else None)
            except Exception:
                new_table = pa.Table.from_pandas(df)
                
            if existing_table is not None:
                combined_table = pa.concat_tables([existing_table, new_table], promote_options='default')
            else:
                combined_table = new_table

            parquet_buffer = BytesIO()
            pq.write_table(combined_table, parquet_buffer)

            self.s3_client.put_object(
                Bucket=self.settings.S3_BUCKET_NAME,
                Key=file_name,
                Body=parquet_buffer.getvalue(),
            )
            logger.info(
                f"S3 upload complete: s3://{self.settings.S3_BUCKET_NAME}/{file_name} "
                f"({combined_table.num_rows} total rows)"
            )

        except Exception as e:
            logger.error(f"Error uploading to S3: {e}")

    # ── Scheduled Tasks ─────────────────────────────────────────────────

    def _fetch_and_publish(self) -> None:
        """
        Main scheduled task: fetch data, enrich, upload to S3, notify callbacks.
        
        This replaces the original final_task1() with added:
        - Holiday awareness
        - LiveCache callback notification
        """
        from app.config.holidays import is_market_open

        if not is_market_open():
            logger.debug("Market is closed, skipping fetch")
            return

        if not self.access_token:
            logger.warning("No access token available, skipping fetch")
            return

        try:
            logger.info("Starting data fetch cycle...")
            final_df = self.fetch_all_data()

            if final_df.empty:
                logger.warning("Fetched DataFrame is empty")
                return

            logger.info(f"Fetched data: {final_df.shape[0]} rows × {final_df.shape[1]} cols")

            # Upload to S3 (existing behavior)
            self.upload_to_s3(final_df)

            # Notify all registered callbacks (LiveCache, WebSocket publisher, etc.)
            for callback in self._on_data_callbacks:
                try:
                    callback(final_df)
                except Exception as e:
                    logger.error(f"Callback {callback.__qualname__} failed: {e}")

            logger.info("Fetch cycle complete")

        except Exception as e:
            logger.error(f"Error in fetch cycle: {e}")

    def _refresh_token(self) -> None:
        """Refresh the Upstox access token (scheduled at 08:58)."""
        self.access_token = self.auto_login()
        if self.access_token:
            logger.info("Access token refreshed successfully")
        else:
            logger.error("Failed to refresh access token")

    # ── Scheduler Lifecycle ─────────────────────────────────────────────

    def start(self) -> None:
        """
        Start the background scheduler.
        
        Schedule:
        - 08:58: Refresh access token
        - 09:00–09:59: Fetch every minute
        - 10:00–14:59: Fetch every minute
        - 15:00–15:30: Fetch every minute
        """
        # Initial login
        self.access_token = self.auto_login()
        if not self.access_token:
            logger.error("Failed to obtain initial access token")
            # Don't return — scheduler will retry via _refresh_token

        self.scheduler = BackgroundScheduler()

        # Data fetch jobs (same schedule as original)
        self.scheduler.add_job(
            self._fetch_and_publish,
            "cron",
            day_of_week="mon-fri",  # Changed from mon-sun to mon-fri
            hour="9",
            minute="0-59",
            id="fetch_9am",
        )
        self.scheduler.add_job(
            self._fetch_and_publish,
            "cron",
            day_of_week="mon-fri",
            hour="10-14",
            minute="*",
            id="fetch_10_14",
        )
        self.scheduler.add_job(
            self._fetch_and_publish,
            "cron",
            day_of_week="mon-fri",
            hour="15",
            minute="0-30",
            id="fetch_15",
        )

        # Token refresh
        self.scheduler.add_job(
            self._refresh_token,
            "cron",
            day_of_week="mon-fri",
            hour="8",
            minute="58",
            id="token_refresh",
        )

        self.scheduler.start()
        logger.info("Scheduler started with 4 cron jobs")

    def stop(self) -> None:
        """Stop the background scheduler gracefully."""
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        """Check if the scheduler is currently running."""
        return self.scheduler is not None and self.scheduler.running
