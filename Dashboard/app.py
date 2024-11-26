from dotenv import load_dotenv
import os
from datetime import datetime, timedelta
from decimal import Decimal
import logging
import re
from functools import wraps
from typing import Dict, Optional
from asyncio import run
import json
import asyncio
import uuid
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
    abort
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


def async_route(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        return run(f(*args, **kwargs))

    return decorated_function


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
        self.company_name = user_data.get("company_name")  # New field for admin's company
        self.invite_code = user_data.get("invite_code")

    def is_admin(self):
        return self.account_type == "admin"

    def can_manage_user(self, user_id):
        """Check if admin can manage a specific user"""
        return (self.is_admin() and 
                user_id in self.managed_users)

    def get_invite_code(self):
        """Retrieve the admin's invite code"""
        return self.invite_code if self.is_admin() else None

    def get_id(self):
        return self.id

    def update_last_login(self):
        mongo.users.update_one(
            {"_id": ObjectId(self.id)}, {"$set": {"last_login": datetime.now()}}
        )

class StockMarketService:
    def __init__(self, finnhub_api_key: str):
        self.finnhub_client = finnhub.Client(os.getenv("FINNHUB_API_KEY"))
        self.cache = {}
        self.cache_timeout = 60  # seconds

    async def get_stock_price(self, symbol: str) -> Optional[float]:
        cache_key = f"{symbol}_price"
        cached_data = self.cache.get(cache_key)
        if (
            cached_data
            and (datetime.now() - cached_data["timestamp"]).seconds < self.cache_timeout
        ):
            return cached_data["price"]

        try:
            # Try Finnhub quote first
            quote = self.finnhub_client.quote(symbol)
            if quote and quote.get('c'):  # Current price
                price = float(quote['c'])
            else:
                raise ValueError("No price data from Finnhub")
        except Exception as finnhub_error:
            logger.warning(f"Finnhub API failed for {symbol}: {finnhub_error}")
            try:
                # Fallback to Yahoo Finance
                stock = yf.Ticker(symbol)
                price = float(stock.history(period="1d")["Close"].iloc[-1])
            except Exception as yf_error:
                logger.error(f"Both APIs failed for {symbol}: {yf_error}")
                return None

        # Update cache
        self.cache[cache_key] = {"price": price, "timestamp": datetime.now()}
        return price

    async def get_stock_info(self, symbol: str) -> Dict:
        """Get detailed stock information using both Finnhub and YFinance data."""
        try:
            # Get the current price first
            price = await self.get_stock_price(symbol)
            if not price:
                raise ValueError(f"Could not fetch price for {symbol}")

            # Try to get details from Finnhub
            try:
                # Fetch company profile
                profile = self.finnhub_client.company_profile2(symbol=symbol)
                
                # Fetch company financials
                financials = self.finnhub_client.company_basic_financials(symbol=symbol, metric='all')
            except Exception as finnhub_error:
                logger.warning(
                    f"Finnhub API failed for ticker details {symbol}: {finnhub_error}"
                )
                profile = {}
                financials = {}

            # Get additional info from YFinance as backup or supplement
            try:
                stock = yf.Ticker(symbol)
                info = stock.info

                # Merge Finnhub and YFinance data
                stock_info = {
                    "symbol": symbol,
                    "price": price,
                    "company_name": profile.get('name') or info.get("longName", ""),
                    "market_cap": profile.get('marketCapitalization') or info.get("marketCap", 0),
                    "pe_ratio": financials.get('metric', {}).get('peBasicExclExtraTTM') or info.get("trailingPE", 0),
                    "volume": info.get("volume", 0),
                    "sector": profile.get('finnhubIndustry') or info.get("sector", ""),
                    "industry": profile.get('finnhubIndustry') or info.get("industry", ""),
                    "website": profile.get('weburl') or info.get("website", ""),
                    "description": profile.get('description') or info.get("longBusinessSummary", ""),
                    "currency": profile.get('currency') or info.get("currency", "USD"),
                    "exchange": profile.get('exchange') or info.get("exchange", ""),
                    "fifty_two_week_high": financials.get('metric', {}).get('52WeekHigh') or info.get("fiftyTwoWeekHigh", 0),
                    "fifty_two_week_low": financials.get('metric', {}).get('52WeekLow') or info.get("fiftyTwoWeekLow", 0),
                    "dividend_yield": financials.get('metric', {}).get('dividendYieldTTM') or info.get("dividendYield", 0),
                    "beta": financials.get('metric', {}).get('beta') or info.get("beta", 0),
                    "last_updated": datetime.now().isoformat(),
                }

                # Get historical data for price chart
                hist = stock.history(period="1mo")
                price_history = [
                    {
                        "date": index.strftime("%Y-%m-%d"),
                        "price": float(row["Close"]),
                        "volume": float(row["Volume"]),
                    }
                    for index, row in hist.iterrows()
                ]
                stock_info["price_history"] = price_history

                return stock_info

            except Exception as yf_error:
                logger.error(f"YFinance API failed for {symbol}: {yf_error}")
                # Return basic info if YFinance fails but we have price
                return {
                    "symbol": symbol,
                    "price": price,
                    "company_name": profile.get('name') or symbol,
                    "market_cap": profile.get('marketCapitalization') or 0,
                    "last_updated": datetime.now().isoformat(),
                }

        except Exception as e:
            logger.error(f"Error fetching stock info for {symbol}: {e}")
            return None

    async def get_market_hours(self, symbol: str) -> Dict:
        """Get market hours information."""
        try:
            # Use Finnhub market hours lookup
            market_status = self.finnhub_client.market_status()
            
            return {
                "is_market_open": market_status.get('marketStatus') == 'open',
                "market": market_status.get('market', 'US'),
            }
        except Exception as e:
            logger.warning(f"Could not fetch market hours from Finnhub: {e}")
            # Fallback to basic US market hours
            now = datetime.now().astimezone()
            is_weekday = now.weekday() < 5
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
            return {
                "is_market_open": is_weekday and market_open <= now <= market_close,
                "next_open": market_open,
                "next_close": market_close,
            }

# Update stock service initialization
stock_service = StockMarketService(
    os.getenv("FINNHUB_API_KEY")  # Change from POLYGON_API_KEY to FINNHUB_API_KEY
)

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

# Custom decorators for error handling and validation
def handle_errors(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {f.__name__}: {str(e)}")
            return jsonify({"success": False, "error": str(e)}), 500

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


# Enhanced routes with better error handling and validation
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
            admin_user = mongo.users.find_one({
                "account_type": "admin", 
                "invite_code": admin_invite_code
            })
            if not admin_user:
                flash("Invalid admin invite code")
                return redirect(url_for("register"))

        # Create user with admin domain if applicable
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
            # Update admin's managed users
            mongo.users.update_one(
                {"_id": admin_user["_id"]},
                {"$push": {"managed_users": str(user_data["_id"])}}
            )

        mongo.users.insert_one(user_data)

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
        # In a real-world scenario, this would be a secure, time-limited, or one-time use key
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

        # Create admin account
        admin_data = {
            "email": email,
            "password": generate_password_hash(password, method="pbkdf2:sha256"),
            "account_type": "admin",
            "company_name": company_name,
            "invite_code": invite_code,
            "managed_users": [],
            "admin_domain": None,  # The admin's own ID will be set as their domain
            "created_at": datetime.now(),
            "last_login": None,
            "registration_ip": request.remote_addr,
        }

        # Insert admin user
        result = mongo.users.insert_one(admin_data)
        admin_id = str(result.inserted_id)

        # Update the admin's domain to be their own ID
        mongo.users.update_one(
            {"_id": result.inserted_id},
            {"$set": {"admin_domain": admin_id}}
        )

        flash("Admin account created successfully")
        return redirect(url_for("login"))

    return render_template("admin_signup.html")


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
            current_price = Decimal(str(stock_info['price']))

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
                    "company_name": stock_info.get('company_name', symbol),
                    "sector": stock_info.get('sector', 'N/A'),
                    "pe_ratio": stock_info.get('pe_ratio', 0),
                    "dividend_yield": stock_info.get('dividend_yield', 0),
                    "market_cap": stock_info.get('market_cap', 0),
                    "price_history": stock_info.get('price_history', []),
                    "last_updated": stock_info.get('last_updated', datetime.now()),
                }
            )

    total_value = balance + total_portfolio_value
    initial_balance = Decimal("10000.00")
    net_profit = total_value - initial_balance

    return render_template(
        "dashboard.html",
        portfolio=sorted(portfolio_data, key=lambda x: x["market_value"], reverse=True),
        balance=balance,
        total_value=total_value,
        net_profit=net_profit,
        net_profit_percent=(net_profit / initial_balance * 100).quantize(
            Decimal("0.01")
        ),
        last_updated=datetime.now(),
        account_type=current_user.account_type,
    )

@app.route("/admin/dashboard")
@login_required
@handle_errors
def admin_dashboard():
    # Ensure only admins can access
    if not current_user.is_admin():
        abort(403)  # Forbidden

    # Fetch users in admin's domain
    managed_users = mongo.users.find({
        "admin_domain": current_user.get_id()
    })

    # Prepare user data for display
    user_list = []
    for user in managed_users:
        user_summary = {
            "id": str(user["_id"]),
            "email": user["email"],
            "balance": float(user.get("balance", "0")),  # Convert to float
            "portfolio_value": _calculate_portfolio_value(user),
            "total_trades": len(user.get("trade_history", [])),
            "last_login": user.get("last_login")
        }
        user_list.append(user_summary)

    return render_template(
        "admin_dashboard.html", 
        users=user_list
    )

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
                current_price = Decimal(str(stock_info['price']))

                market_value = shares * current_price
                total_portfolio_value += market_value

                portfolio_details.append({
                    "symbol": symbol,
                    "shares": shares,
                    "avg_price": avg_price,
                    "current_price": current_price,
                    "market_value": market_value,
                    "company_name": stock_info.get('company_name', symbol)
                })
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
        trade_history=trade_history
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
        mongo.users.update_one(
            {"_id": ObjectId(user_id)}, 
            update_operation
        )

        # Log balance adjustment in trade history
        mongo.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$push": {
                "trade_history": {
                    "type": f"admin_{action}_balance",
                    "amount": float(amount),  # Convert to float for storage
                    "timestamp": datetime.now(),
                    "admin_id": current_user.get_id()
                }
            }}
        )

        flash(f"Balance {action}ed successfully")
        return redirect(url_for("admin_dashboard"))

    except (ValueError, TypeError):
        flash("Invalid amount")
        return redirect(url_for("admin_dashboard"))

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

    mongo.users.insert_one({
        "email": email,
        "password": generate_password_hash(password, method="pbkdf2:sha256"),
        "account_type": "admin",
        "invite_code": invite_code,
        "managed_users": [],
        "created_at": datetime.now(),
        "last_login": None
    })

    return jsonify({
        "message": "Admin created successfully", 
        "invite_code": invite_code
    })
    
def _calculate_portfolio_value(user_data):
    """Helper function to calculate total portfolio value"""
    portfolio = user_data.get("portfolio", {})
    total_value = Decimal("0")

    for symbol, position in portfolio.items():
        if position["shares"] > 0:
            try:
                stock_info = stock_service.get_stock_info(symbol)
                current_price = Decimal(str(stock_info['price']))
                shares = Decimal(str(position["shares"]))
                total_value += shares * current_price
            except Exception:
                # Handle potential errors in stock info retrieval
                continue

    return total_value

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
    if not current_price or abs(Decimal(str(current_price)) - price) / price > Decimal(
        "0.02"
    ):
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
                            "shares": float(
                                total_shares
                            ),  # Convert to float for MongoDB
                            "avg_price": float(
                                new_avg_price
                            ),  # Convert to float for MongoDB
                            "last_updated": datetime.now(),
                        },
                        "balance": float(
                            current_balance - total_cost
                        ),  # Convert to float for MongoDB
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
    if not current_price or abs(Decimal(str(current_price)) - price) / price > Decimal(
        "0.02"
    ):
        return jsonify(
            {
                "success": False,
                "error": "Price has changed significantly. Please refresh and try again.",
            }
        )

    total_value = shares * price

    # Use MongoDB transaction for atomicity
    with db_manager.client.start_session() as session:  # Use db_manager.client here
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


@app.route("/portfolio_analysis")
@login_required
@handle_errors
@async_route
async def portfolio_analysis():
    user_data = mongo.users.find_one({"_id": ObjectId(current_user.get_id())})
    portfolio = user_data.get("portfolio", {})
    trade_history = user_data.get("trade_history", [])

    # Calculate portfolio metrics
    total_value = Decimal("0")
    total_cost = Decimal("0")
    sector_exposure = {}
    risk_metrics = {}

    for symbol, position in portfolio.items():
        if position["shares"] > 0:
            current_price = await stock_service.get_stock_price(symbol)
            if current_price:
                shares = Decimal(str(position["shares"]))
                price = Decimal(str(current_price))
                avg_price = Decimal(str(position["avg_price"]))

                position_value = shares * price
                position_cost = shares * avg_price

                total_value += position_value
                total_cost += position_cost

                # Get sector information
                try:
                    stock = yf.Ticker(symbol)
                    sector = stock.info.get("sector", "Unknown")
                    sector_exposure[sector] = (
                        sector_exposure.get(sector, Decimal("0")) + position_value
                    )
                except Exception:
                    sector_exposure["Unknown"] = (
                        sector_exposure.get("Unknown", Decimal("0")) + position_value
                    )

    # Calculate trading metrics
    trades_analysis = {
        "total_trades": len(trade_history),
        "winning_trades": sum(
            1 for trade in trade_history if trade.get("realized_pl", 0) > 0
        ),
        "losing_trades": sum(
            1 for trade in trade_history if trade.get("realized_pl", 0) < 0
        ),
        "total_realized_pl": sum(
            Decimal(str(trade.get("realized_pl", 0))) for trade in trade_history
        ),
        "average_trade_size": (
            Decimal(
                str(sum(trade["total"] for trade in trade_history) / len(trade_history))
            )
            if trade_history
            else Decimal("0")
        ),
    }

    return render_template(
        "portfolio_analysis.html",
        total_value=float(total_value),
        total_cost=float(total_cost),
        total_return=(
            float((total_value - total_cost) / total_cost * 100) if total_cost else 0
        ),
        sector_exposure={k: float(v) for k, v in sector_exposure.items()},
        trades_analysis={
            k: float(v) if isinstance(v, Decimal) else v
            for k, v in trades_analysis.items()
        },
        risk_metrics=risk_metrics,
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


# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(error):
    db_manager.client.session.rollback()  # Rollback the session in case of database error
    return render_template("500.html"), 500


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


# WebSocket support for real-time updates
from flask_socketio import SocketIO, emit, join_room

socketio = SocketIO(app)


async def send_price_updates():
    while True:
        for user in mongo.users.find({}):
            portfolio = user.get("portfolio", {})
            updates = {}

            for symbol in portfolio.keys():
                price = await stock_service.get_stock_price(symbol)
                if price:
                    updates[symbol] = float(price)

            if updates:
                emit("price_updates", updates, room=str(user["_id"]))

        await asyncio.sleep(5)  # Update every 5 seconds


@socketio.on("connect")
@login_required
def handle_connect():
    # Join a room specific to this user
    join_room(current_user.get_id())


# Run the app
if __name__ == "__main__":
    # For development use
    socketio.run(app, debug=True, port=8080, host='0.0.0.0', allow_unsafe_werkzeug=True)
