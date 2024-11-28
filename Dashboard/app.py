# Standard library imports
import os
from datetime import datetime, timedelta
from decimal import Decimal
import logging
import re
import json
import uuid
from functools import wraps
from typing import Dict, Optional, List, Any
from asyncio import run
import asyncio
from concurrent.futures import ThreadPoolExecutor

# Third-party library imports
from dotenv import load_dotenv
import finnhub
import yfinance as yf
from bson import ObjectId
from bson.decimal128 import Decimal128
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    abort,
)
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user,
)
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from werkzeug.security import generate_password_hash, check_password_hash


# Load environment variables from the .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask App Setup with improved configuration
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY"),  # Use SECRET_KEY from environment variable
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=1),
)

# After your Flask app initialization
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = (
    "login"  # Specify what route to redirect to when login is required
)
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id):
    user_data = mongo.users.find_one({"_id": ObjectId(user_id)})
    if user_data:
        return User(user_data)
    return None


# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return render_template("404.html"), 404


@app.errorhandler(403)
def forbidden_error(error):
    return render_template("403.html"), 403


def handle_errors(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {f.__name__}: {str(e)}")
            return jsonify({"success": False, "error": str(e)}), 500

    return decorated_function


def async_route(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        return run(f(*args, **kwargs))

    return decorated_function

def validate_stock_input(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        data = request.json
        required_fields = ["symbol", "shares", "price"]

        # Validate required fields
        if not all(field in data for field in required_fields):
            return (
                jsonify({"success": False, "error": "Missing required fields"}),
                400,
            )

        # Validate symbol format
        if not re.match(r"^[A-Z]{1,5}$", data["symbol"].upper()):
            return (
                jsonify({"success": False, "error": "Invalid symbol format"}),
                400,
            )

        # Validate numeric values
        try:
            shares = float(data["shares"])
            price = float(data["price"])
            if shares <= 0 or price <= 0:
                raise ValueError
        except ValueError:
            return (
                jsonify({"success": False, "error": "Invalid numeric values"}),
                400,
            )

        return f(*args, **kwargs)

    return decorated_function


# API rate limiting
def rate_limit(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_id = current_user.get_id()
        current_time = datetime.now()

        # Check rate limit (100 requests per minute)
        rate_limit_key = f"rate_limit:{user_id}"
        request_times = mongo.rate_limits.find_one({"_id": rate_limit_key})

        if request_times:
            times = request_times["times"]
            # Remove timestamps older than 1 minute
            times = [t for t in times if (current_time - t).total_seconds() < 60]

            if len(times) >= 100:
                return jsonify({"error": "Rate limit exceeded"}), 429

            times.append(current_time)
            mongo.rate_limits.update_one(
                {"_id": rate_limit_key}, {"$set": {"times": times}}
            )
        else:
            mongo.rate_limits.insert_one(
                {"_id": rate_limit_key, "times": [current_time]}
            )

        return f(*args, **kwargs)

    return decorated_function


# Create a JSON encoder that handles Decimal
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, Decimal128):
            return str(obj)
        return super().default(obj)


# Update the Flask app configuration
app.json_encoder = CustomJSONEncoder


# MongoDB Setup with connection pooling and error handling
class DatabaseManager:
    def __init__(self, uri: str):
        self.uri = uri
        self.client = None
        self.db = None
        self.connect()

    def connect(self):
        try:
            self.client = MongoClient(
                self.uri,
                server_api=ServerApi("1"),
                maxPoolSize=50,
                connectTimeoutMS=5000,
                retryWrites=True,
            )
            self.db = self.client["StockDash"]
            # Test connection
            self.client.admin.command("ping")
            logger.info("Successfully connected to MongoDB")
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise

    def get_db(self):
        if not self.client:
            self.connect()
        return self.db


db_manager = DatabaseManager(
    os.getenv("DATABASE_URI")
)  # Use DATABASE_URI from environment variable
mongo = db_manager.get_db()


# Enhanced User model with additional features
class User(UserMixin):
    def __init__(self, user_data: dict):
        self.user_data = user_data
        self.id = str(user_data["_id"])
        self.email = user_data["email"]
        self.balance = Decimal(str(user_data.get("balance", "0")))
        self.portfolio = user_data.get("portfolio", {})
        self.trade_history = user_data.get("trade_history", [])
        self.last_login = user_data.get("last_login")

        # Enhanced account type handling
        self.account_type = user_data.get("account_type", "basic")

        # New admin-related fields
        self.admin_domain = user_data.get("admin_domain", None)
        self.managed_users = user_data.get("managed_users", [])
        self.company_name = user_data.get(
            "company_name"
        )  # New field for admin's company
        self.invite_code = user_data.get("invite_code")

    def is_admin(self):
        return self.account_type == "admin"

    def can_manage_user(self, user_id):
        """Check if admin can manage a specific user"""
        return self.is_admin() and user_id in self.managed_users

    def get_invite_code(self):
        """Retrieve the admin's invite code"""
        return self.invite_code if self.is_admin() else None

    def get_id(self):
        return self.id

    def update_last_login(self):
        mongo.users.update_one(
            {"_id": ObjectId(self.id)}, {"$set": {"last_login": datetime.now()}}
        )


@app.route("/register", methods=["GET", "POST"])
@handle_errors
def register():
    if request.method == "POST":
        email = request.form["email"].lower().strip()
        password = request.form["password"]
        admin_invite_code = request.form.get("admin_invite_code", "")

        # Enhanced input validation
        if not re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email):
            flash("Invalid email format")
            return redirect(url_for("register"))

        if len(password) < 8:
            flash("Password must be at least 8 characters long")
            return redirect(url_for("register"))

        if mongo.users.find_one({"email": email}):
            flash("Email already exists")
            return redirect(url_for("register"))

        # Check admin invite code if provided
        admin_user = None
        if admin_invite_code:
            admin_user = mongo.users.find_one(
                {"account_type": "admin", "invite_code": admin_invite_code}
            )
            if not admin_user:
                flash("Invalid admin invite code")
                return redirect(url_for("register"))

        # Create user data
        user_data = {
            "email": email,
            "password": generate_password_hash(password, method="pbkdf2:sha256"),
            "balance": Decimal128(Decimal("10000.00")),
            "portfolio": {},
            "trade_history": [],
            "created_at": datetime.now(),
            "last_login": None,
            "account_type": "basic",
            "login_attempts": 0,
        }

        # If registering through admin invite, set admin domain
        if admin_user:
            user_data["admin_domain"] = str(admin_user["_id"])

        # Insert the new user into the database
        new_user = mongo.users.insert_one(user_data)

        if admin_user:
            mongo.users.update_one(
                {"_id": admin_user["_id"]},
                {"$push": {"managed_users": str(new_user.inserted_id)}},
            )

        flash("Registration successful")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/admin/signup", methods=["GET", "POST"])
def admin_signup():
    if request.method == "POST":
        # Collect admin signup information
        company_name = request.form["company_name"].strip()
        email = request.form["email"].lower().strip()
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]
        admin_registration_key = request.form["admin_registration_key"]

        # Validation checks
        if not company_name:
            flash("Company name is required")
            return redirect(url_for("admin_signup"))

        if not re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email):
            flash("Invalid email format")
            return redirect(url_for("admin_signup"))

        if password != confirm_password:
            flash("Passwords do not match")
            return redirect(url_for("admin_signup"))

        if len(password) < 12:
            flash("Password must be at least 12 characters long")
            return redirect(url_for("admin_signup"))

        # Check if admin registration key is valid
        if admin_registration_key != os.getenv("admin_registration_key"):
            flash("Invalid admin registration key")
            return redirect(url_for("admin_signup"))

        # Check if email already exists
        existing_user = mongo.users.find_one({"email": email})
        if existing_user:
            flash("Email already exists")
            return redirect(url_for("admin_signup"))

        # Generate unique invite code for the admin's domain
        invite_code = str(uuid.uuid4())

        # Create admin account with all fields from normal registration
        admin_data = {
            "email": email,
            "password": generate_password_hash(password, method="pbkdf2:sha256"),
            "balance": Decimal128(Decimal("10000.00")),
            "portfolio": {},
            "trade_history": [],
            "created_at": datetime.now(),
            "last_login": None,
            "account_type": "admin",
            "login_attempts": 0,
            # Admin-specific fields
            "company_name": company_name,
            "invite_code": invite_code,
            "managed_users": [],
            "admin_domain": None,
            "registration_ip": request.remote_addr,
        }

        # Insert admin user
        result = mongo.users.insert_one(admin_data)
        admin_id = str(result.inserted_id)

        # Update the admin's domain to be their own ID and add themselves to managed_users
        mongo.users.update_one(
            {"_id": result.inserted_id},
            {
                "$set": {
                    "admin_domain": admin_id,
                    "managed_users": [admin_id],  # Add admin's own ID to managed_users
                }
            },
        )

        flash("Admin account created successfully")
        return redirect(url_for("login"))

    return render_template("admin_signup.html")


# Modify admin creation route
@app.route("/create_admin", methods=["POST"])
def create_admin():
    # This should be a protected route, potentially with a master admin key
    admin_key = request.form.get("admin_key")
    email = request.form["email"].lower().strip()
    password = request.form["password"]

    # Validate master admin key (you should have a secure way to manage this)
    if admin_key != "YOUR_SECURE_ADMIN_CREATION_KEY":
        abort(403)

    # Generate a unique invite code for this admin
    invite_code = str(uuid.uuid4())

    mongo.users.insert_one(
        {
            "email": email,
            "password": generate_password_hash(password, method="pbkdf2:sha256"),
            "account_type": "admin",
            "invite_code": invite_code,
            "managed_users": [],
            "created_at": datetime.now(),
            "last_login": None,
        }
    )

    return jsonify(
        {"message": "Admin created successfully", "invite_code": invite_code}
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].lower().strip()
        password = request.form["password"]
        remember = "remember_me" in request.form

        user_data = mongo.users.find_one({"email": email})
        if user_data and check_password_hash(user_data["password"], password):
            user = User(user_data)
            login_user(user, remember=remember)
            user.update_last_login()
            return redirect(url_for("dashboard"))

        flash("Invalid email or password")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/admin/dashboard")
@login_required
@handle_errors
def admin_dashboard():
    # Ensure only admins can access
    if not current_user.is_admin():
        abort(403)  # Forbidden

    # Fetch users in admin's domain
    managed_users = mongo.users.find({"admin_domain": current_user.get_id()})

    # Prepare user data for display
    user_list = []
    for user in managed_users:
        user_summary = {
            "id": str(user["_id"]),
            "email": user["email"],
            "balance": float(user.get("balance", Decimal128("0")).to_decimal()),
            "portfolio_value": _calculate_portfolio_value(user),
            "total_trades": len(user.get("trade_history", [])),
            "last_login": user.get("last_login"),
        }
        user_list.append(user_summary)

    return render_template("admin_dashboard.html", users=user_list)


@app.route("/admin/user/<user_id>")
@login_required
@handle_errors
def inspect_user(user_id):
    # Ensure only admins can access and only their domain users
    if not current_user.is_admin() or not current_user.can_manage_user(user_id):
        abort(403)  # Forbidden

    # Fetch user details
    user_data = mongo.users.find_one({"_id": ObjectId(user_id)})
    if not user_data:
        abort(404)  # Not Found

    # Prepare detailed user information
    portfolio = user_data.get("portfolio", {})
    portfolio_details = []
    total_portfolio_value = Decimal("0")

    for symbol, position in portfolio.items():
        if position["shares"] > 0:
            try:
                stock_info = stock_service.get_stock_info(symbol)
                shares = Decimal(str(position["shares"]))
                avg_price = Decimal(str(position["avg_price"]))
                current_price = Decimal(str(stock_info["price"]))

                market_value = shares * current_price
                total_portfolio_value += market_value

                portfolio_details.append(
                    {
                        "symbol": symbol,
                        "shares": shares,
                        "avg_price": avg_price,
                        "current_price": current_price,
                        "market_value": market_value,
                        "company_name": stock_info.get("company_name", symbol),
                    }
                )
            except Exception:
                continue

    # Fetch trade history
    trade_history = user_data.get("trade_history", [])

    return render_template(
        "admin_user_inspect.html",
        user_email=user_data["email"],
        balance=Decimal(str(user_data.get("balance", "0"))),
        total_portfolio_value=total_portfolio_value,
        portfolio=portfolio_details,
        trade_history=trade_history,
    )


@app.route("/admin/adjust_balance", methods=["POST"])
@login_required
@handle_errors
def adjust_balance():
    # Ensure only admins can adjust balance
    if not current_user.is_admin():
        abort(403)  # Forbidden

    user_id = request.form.get("user_id")
    amount = request.form.get("amount")
    action = request.form.get("action")  # "add" or "subtract"

    # Validate inputs
    if not user_id or not amount:
        flash("Invalid input")
        return redirect(url_for("admin_dashboard"))

    try:
        # Convert to Decimal
        amount = Decimal(amount)

        # Ensure the admin can manage this user
        if not current_user.can_manage_user(user_id):
            abort(403)

        # Prepare update operation with Decimal128
        update_operation = (
            {"$inc": {"balance": Decimal128(amount)}}
            if action == "add"
            else {"$inc": {"balance": Decimal128(-amount)}}
        )

        # Update user's balance and log the transaction
        mongo.users.update_one({"_id": ObjectId(user_id)}, update_operation)

        # Log balance adjustment in trade history
        mongo.users.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$push": {
                    "trade_history": {
                        "type": f"admin_{action}_balance",
                        "amount": float(amount),  # Convert to float for storage
                        "timestamp": datetime.now(),
                        "admin_id": current_user.get_id(),
                    }
                }
            },
        )

        flash(f"Balance {action}ed successfully")
        return redirect(url_for("admin_dashboard"))

    except (ValueError, TypeError):
        flash("Invalid amount")
        return redirect(url_for("admin_dashboard"))


def _calculate_portfolio_value(user_data):
    """Helper function to calculate total portfolio value"""
    portfolio = user_data.get("portfolio", {})
    total_value = Decimal("0")

    for symbol, position in portfolio.items():
        if position["shares"] > 0:
            try:
                stock_info = stock_service.get_stock_info(symbol)
                current_price = Decimal(str(stock_info["price"]))
                shares = Decimal(str(position["shares"]))
                total_value += shares * current_price
            except Exception:
                # Handle potential errors in stock info retrieval
                continue

    return total_value

# Constants
CACHE_TIMEOUT = 60  # seconds

class StockMarketService:
    def __init__(self, finnhub_api_key: str = None):
        """
        Initialize StockMarketService with API keys and configurations.
        
        Args:
            finnhub_api_key (str, optional): Finnhub API key. 
                                             If not provided, attempts to use environment variable.
        """
        api_key = finnhub_api_key or os.getenv("FINNHUB_API_KEY")
        if not api_key:
            raise ValueError("No Finnhub API key provided. Set FINNHUB_API_KEY environment variable.")
        
        self.finnhub_client = finnhub.Client(api_key=api_key)
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.executor = ThreadPoolExecutor()

    def _update_cache(self, key: str, value: Any) -> None:
        """Update cache with a timestamp."""
        self.cache[key] = {"data": value, "timestamp": datetime.now()}

    def _get_cached(self, key: str) -> Optional[Any]:
        """Retrieve data from the cache if valid."""
        cached_entry = self.cache.get(key)
        if cached_entry:
            if (datetime.now() - cached_entry["timestamp"]).seconds < CACHE_TIMEOUT:
                return cached_entry["data"]
            else:
                # Expire old cache
                del self.cache[key]
        return None

    async def _fetch_yf_data(self, symbol: str, method: str, *args, **kwargs) -> Any:
        """Fetch data asynchronously using yfinance."""
        try:
            if method == "info":
                return yf.Ticker(symbol).info  # Directly return info as it is not a callable method
            return await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: getattr(yf.Ticker(symbol), method)(*args, **kwargs)
            )
        except Exception as e:
            logger.error(f"Error fetching {method} for {symbol} via yfinance: {e}")
            return None

    async def get_stock_price(self, symbol: str) -> Optional[float]:
        """Fetch the latest stock price, prioritizing Finnhub."""
        cache_key = f"{symbol}_price"
        cached_price = self._get_cached(cache_key)
        if cached_price is not None:
            return cached_price

        # Try Finnhub API
        try:
            quote = self.finnhub_client.quote(symbol)
            if quote and (current_price := quote.get("c")):
                price = float(current_price)
                self._update_cache(cache_key, price)
                return price
        except Exception as e:
            logger.warning(f"Finnhub API failed for {symbol}: {e}")

        # Fallback to Yahoo Finance
        try:
            history = await self._fetch_yf_data(symbol, "history", period="1d")
            if history is not None and not history.empty:
                price = float(history["Close"].iloc[-1])
                self._update_cache(cache_key, price)
                return price
        except Exception as e:
            logger.error(f"YFinance API failed for {symbol}: {e}")

        return None

    async def get_stock_price_history(
        self, symbol: str, period: str = "1mo", interval: str = "1d"
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch historical stock prices for a given symbol."""
        cache_key = f"{symbol}_history_{period}_{interval}"
        cached_history = self._get_cached(cache_key)
        if cached_history is not None:
            return cached_history

        try:
            history = await self._fetch_yf_data(symbol, "history", period=period, interval=interval)
            if history is not None and not history.empty:
                historical_data = [
                    {"date": index.strftime("%Y-%m-%d"), "price": float(row["Close"]), "volume": int(row["Volume"])}
                    for index, row in history.iterrows()
                ]
                self._update_cache(cache_key, historical_data)
                return historical_data
        except Exception as e:
            logger.error(f"Failed to fetch historical data for {symbol}: {e}")

        return None

    async def get_stock_info(self, symbol: str) -> Optional[Dict]:
        """Fetch comprehensive stock information."""
        try:
            price = await self.get_stock_price(symbol)
            if price is None:
                logger.error(f"Price data unavailable for {symbol}.")
                return None

            stock_info = {"symbol": symbol, "price": price, "last_updated": datetime.now().isoformat()}
            profile, financials = {}, {}

            # Try Finnhub API
            try:
                profile = self.finnhub_client.company_profile2(symbol=symbol)
                financials = self.finnhub_client.company_basic_financials(symbol=symbol, metric="all")
            except Exception as e:
                logger.warning(f"Finnhub profile/financials fetch failed for {symbol}: {e}")

            # Try YFinance fallback
            try:
                info = await self._fetch_yf_data(symbol, "info")
                history = await self.get_stock_price_history(symbol, period="1mo")

                stock_info.update({
                    "company_name": profile.get("name", info.get("longName", "")),
                    "market_cap": profile.get("marketCapitalization", info.get("marketCap", 0)),
                    "pe_ratio": financials.get("metric", {}).get("peBasicExclExtraTTM", info.get("trailingPE", 0)),
                    "sector": profile.get("finnhubIndustry", info.get("sector", "N/A")),
                    "exchange": profile.get("exchange", info.get("exchange", "N/A")),
                    "currency": profile.get("currency", "USD"),
                    "fifty_two_week_high": financials.get("metric", {}).get("52WeekHigh", info.get("fiftyTwoWeekHigh", 0)),
                    "fifty_two_week_low": financials.get("metric", {}).get("52WeekLow", info.get("fiftyTwoWeekLow", 0)),
                    "price_history": history,
                    "website": profile.get("weburl", info.get("website", "")),
                    "description": profile.get("description", info.get("longBusinessSummary", "")),
                    "volume": info.get("volume", 0),
                })
            except Exception as e:
                logger.error(f"YFinance fallback failed for {symbol}: {e}")

            return stock_info
        except Exception as e:
            logger.error(f"Failed to fetch stock info for {symbol}: {e}")
            return None

    async def get_market_news(self, category: str = "general", min_id: int = 0) -> List[Dict]:
        """Fetch general market news from Finnhub."""
        try:
            news = self.finnhub_client.general_news(category=category, min_id=min_id)
            return [
                {
                    "headline": article.get("headline"),
                    "source": article.get("source"),
                    "url": article.get("url"),
                    "summary": article.get("summary"),
                    "image": article.get("image"),
                    "datetime": datetime.fromtimestamp(article["datetime"]).isoformat(),
                }
                for article in news
            ]
        except Exception as e:
            logger.error(f"Error fetching market news: {e}")
            return []

        
stock_service = StockMarketService(finnhub_api_key=os.getenv("FINNHUB_API_KEY"))

@app.route("/dashboard")
@login_required
@handle_errors
@async_route
async def dashboard():
    user_data = mongo.users.find_one({"_id": ObjectId(current_user.get_id())})
    portfolio = user_data.get("portfolio", {})
    balance = Decimal(str(user_data.get("balance", "0")))

    portfolio_data = []
    total_portfolio_value = Decimal("0")

    # Fetch comprehensive stock info concurrently
    async def fetch_all_stock_details():
        tasks = []
        for symbol in portfolio.keys():
            if portfolio[symbol]["shares"] > 0:
                tasks.append(stock_service.get_stock_info(symbol))
        return await asyncio.gather(*tasks)

    stock_details = await fetch_all_stock_details()

    for (symbol, position), stock_info in zip(portfolio.items(), stock_details):
        if position["shares"] > 0 and stock_info:
            shares = Decimal(str(position["shares"]))
            avg_price = Decimal(str(position["avg_price"]))
            current_price = Decimal(str(stock_info["price"]))

            market_value = shares * current_price
            total_portfolio_value += market_value
            cost_basis = shares * avg_price

            portfolio_data.append(
                {
                    "symbol": symbol,
                    "shares": shares,
                    "avg_price": avg_price,
                    "current_price": current_price,
                    "market_value": market_value,
                    "profit_loss": market_value - cost_basis,
                    "profit_loss_percent": (
                        (current_price - avg_price) / avg_price * 100
                    ).quantize(Decimal("0.01")),
                    
                    # New enhanced stock information from StockMarketService
                    "company_name": stock_info.get("company_name", symbol),
                    "sector": stock_info.get("sector", "N/A"),
                    "pe_ratio": stock_info.get("pe_ratio", 0),
                    "market_cap": stock_info.get("market_cap", 0),
                    "exchange": stock_info.get("exchange", "N/A"),
                    "currency": stock_info.get("currency", "USD"),
                    
                    # Additional financial metrics
                    "fifty_two_week_high": stock_info.get("fifty_two_week_high", 0),
                    "fifty_two_week_low": stock_info.get("fifty_two_week_low", 0),
                    
                    # Detailed price history
                    "price_history": stock_info.get("price_history", []),
                    
                    # Company description and website
                    "description": stock_info.get("description", ""),
                    "website": stock_info.get("website", ""),
                    
                    # Volume and last updated timestamp
                    "volume": stock_info.get("volume", 0),
                    "last_updated": stock_info.get("last_updated", datetime.now()),
                }
            )

    # Sort portfolio by market value
    portfolio_data.sort(key=lambda x: x["market_value"], reverse=True)

    # Calculate total portfolio metrics
    total_value = balance + total_portfolio_value
    initial_balance = Decimal("10000.00")
    net_profit = total_value - initial_balance

    # Fetch some market news for dashboard context
    try:
        market_news = await stock_service.get_market_news()
    except Exception:
        market_news = []

    return render_template(
        "dashboard.html",
        portfolio=portfolio_data,
        balance=balance,
        total_value=total_value,
        net_profit=net_profit,
        net_profit_percent=(net_profit / initial_balance * 100).quantize(
            Decimal("0.01")
        ),
        market_news=market_news,  # New addition of market news
        last_updated=datetime.now(),
        account_type=current_user.account_type,
    )

@app.route("/get_stock_info/<symbol>")
@login_required
@async_route
@handle_errors
async def get_stock_info(symbol: str):
    symbol = symbol.upper()
    stock_info = await stock_service.get_stock_info(symbol)

    if not stock_info:
        return (
            jsonify(
                {"success": False, "error": f"Could not fetch information for {symbol}"}
            ),
            404,
        )

    return jsonify({**stock_info, "success": True})


@app.route("/buy_stock", methods=["POST"])
@login_required
@handle_errors
@async_route
@validate_stock_input
async def buy_stock():
    data = request.json
    symbol = data["symbol"].upper()
    shares = Decimal(str(data["shares"]))
    price = Decimal(str(data["price"]))
    reason = data.get("reason", "")

    # Verify current price
    current_price = await stock_service.get_stock_price(symbol)
    if not current_price or abs(Decimal(str(current_price)) - price) / price > Decimal("0.02"):
        return jsonify(
            {
                "success": False,
                "error": "Price has changed significantly. Please refresh and try again.",
            }
        )

    total_cost = shares * price

    # Use MongoDB transaction for atomicity
    with db_manager.client.start_session() as session:
        with session.start_transaction():
            user_data = mongo.users.find_one({"_id": ObjectId(current_user.get_id())})
            current_balance = Decimal(str(user_data["balance"]))

            if total_cost > current_balance:
                return jsonify({"success": False, "error": "Insufficient funds"})

            portfolio = user_data.get("portfolio", {})
            current_position = portfolio.get(
                symbol, {"shares": Decimal("0"), "avg_price": Decimal("0")}
            )

            # Calculate new position
            total_shares = Decimal(str(current_position["shares"])) + shares
            new_avg_price = (
                (
                    Decimal(str(current_position["shares"]))
                    * Decimal(str(current_position["avg_price"]))
                )
                + (shares * price)
            ) / total_shares

            # Update user's portfolio and balance
            mongo.users.update_one(
                {"_id": ObjectId(current_user.get_id())},
                {
                    "$set": {
                        f"portfolio.{symbol}": {
                            "shares": float(total_shares),  # Convert to float for MongoDB
                            "avg_price": float(new_avg_price),  # Convert to float for MongoDB
                            "last_updated": datetime.now(),
                        },
                        "balance": float(current_balance - total_cost),  # Convert to float for MongoDB
                    },
                    "$push": {
                        "trade_history": {
                            "type": "buy",
                            "symbol": symbol,
                            "shares": float(shares),
                            "price": float(price),
                            "total": float(total_cost),
                            "reason": reason,
                            "timestamp": datetime.now(),
                        }
                    },
                },
                session=session,
            )

    return jsonify(
        {
            "success": True,
            "new_balance": float(current_balance - total_cost),
            "transaction": {
                "type": "buy",
                "symbol": symbol,
                "shares": float(shares),
                "price": float(price),
                "total": float(total_cost),
            },
        }
    )


@app.route("/sell_stock", methods=["POST"])
@login_required
@handle_errors
@async_route
@validate_stock_input
async def sell_stock():
    data = request.json
    symbol = data["symbol"].upper()
    shares = Decimal(str(data["shares"]))
    price = Decimal(str(data["price"]))

    # Verify current price
    current_price = await stock_service.get_stock_price(symbol)
    if not current_price or abs(Decimal(str(current_price)) - price) / price > Decimal("0.02"):
        return jsonify(
            {
                "success": False,
                "error": "Price has changed significantly. Please refresh and try again.",
            }
        )

    total_value = shares * price

    # Use MongoDB transaction for atomicity
    with db_manager.client.start_session() as session:
        with session.start_transaction():
            user_data = mongo.users.find_one({"_id": ObjectId(current_user.get_id())})
            portfolio = user_data.get("portfolio", {})

            if symbol not in portfolio or portfolio[symbol]["shares"] < shares:
                return jsonify({"success": False, "error": "Insufficient shares"})

            current_shares = Decimal(str(portfolio[symbol]["shares"]))
            new_shares = current_shares - shares
            current_balance = Decimal(str(user_data["balance"]))

            update_data = {
                "balance": current_balance + total_value,
            }

            if new_shares > 0:
                update_data[f"portfolio.{symbol}.shares"] = new_shares
                update_data[f"portfolio.{symbol}.last_updated"] = datetime.now()
            else:
                update_data[f"portfolio.{symbol}"] = None

            # Calculate realized profit/loss
            avg_price = Decimal(str(portfolio[symbol]["avg_price"]))
            realized_pl = (price - avg_price) * shares

            mongo.users.update_one(
                {"_id": ObjectId(current_user.get_id())},
                {
                    "$set": update_data,
                    "$push": {
                        "trade_history": {
                            "type": "sell",
                            "symbol": symbol,
                            "shares": float(shares),
                            "price": float(price),
                            "total": float(total_value),
                            "realized_pl": float(realized_pl),
                            "timestamp": datetime.now(),
                        }
                    },
                },
                session=session,
            )

    return jsonify(
        {
            "success": True,
            "new_balance": float(current_balance + total_value),
            "transaction": {
                "type": "sell",
                "symbol": symbol,
                "shares": float(shares),
                "price": float(price),
                "total": float(total_value),
                "realized_pl": float(realized_pl),
            },
        }
    )

@app.route("/trade_history")
@login_required
@handle_errors
def trade_history():
    page = request.args.get("page", 1, type=int)
    per_page = 20

    user_data = mongo.users.find_one({"_id": ObjectId(current_user.get_id())})
    trade_history = user_data.get("trade_history", [])

    # Sort trades by timestamp (most recent first)
    sorted_trades = sorted(trade_history, key=lambda x: x["timestamp"], reverse=True)

    # Implement pagination
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_trades = sorted_trades[start_idx:end_idx]

    total_pages = (len(sorted_trades) + per_page - 1) // per_page

    return render_template(
        "trade_history.html",
        trades=paginated_trades,
        page=page,
        total_pages=total_pages,
        has_prev=page > 1,
        has_next=page < total_pages,
    )

if __name__ == "__main__":
    app.run(debug=True, port=8080, host="0.0.0.0")
