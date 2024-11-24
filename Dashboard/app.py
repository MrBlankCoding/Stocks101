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
)
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user,
)
from polygon import RESTClient
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
        self.account_type = user_data.get("account_type", "basic")

    def get_id(self):
        return self.id

    def update_last_login(self):
        mongo.users.update_one(
            {"_id": ObjectId(self.id)}, {"$set": {"last_login": datetime.now()}}
        )


class StockMarketService:
    def __init__(self, polygon_api_key: str):
        self.polygon_client = RESTClient(polygon_api_key)
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
            # Modified Polygon API call for basic plan
            aggs = self.polygon_client.get_previous_close_agg(symbol)
            if isinstance(aggs, list) and aggs:  # Check if it's a list and not empty
                price = float(aggs[0].close)
            else:
                raise ValueError("No data returned from Polygon")
        except Exception as polygon_error:
            logger.warning(f"Polygon API failed for {symbol}: {polygon_error}")
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
        """Get detailed stock information using both Polygon and YFinance data."""
        try:
            # Get the current price first
            price = await self.get_stock_price(symbol)
            if not price:
                raise ValueError(f"Could not fetch price for {symbol}")

            # Try to get details from Polygon first
            try:
                ticker_details = self.polygon_client.get_ticker_details(symbol)
                company_name = (
                    ticker_details.name if hasattr(ticker_details, "name") else None
                )
                market_cap = (
                    ticker_details.market_cap
                    if hasattr(ticker_details, "market_cap")
                    else None
                )
            except Exception as polygon_error:
                logger.warning(
                    f"Polygon API failed for ticker details {symbol}: {polygon_error}"
                )
                company_name = None
                market_cap = None

            # Get additional info from YFinance as backup or supplement
            try:
                stock = yf.Ticker(symbol)
                info = stock.info

                # Use Polygon data if available, otherwise fall back to YFinance
                company_name = company_name or info.get("longName", "")
                market_cap = market_cap or info.get("marketCap", 0)

                stock_info = {
                    "symbol": symbol,
                    "price": price,
                    "company_name": company_name,
                    "market_cap": market_cap,
                    "pe_ratio": info.get("trailingPE", 0),
                    "volume": info.get("volume", 0),
                    "sector": info.get("sector", ""),
                    "industry": info.get("industry", ""),
                    "website": info.get("website", ""),
                    "description": info.get("longBusinessSummary", ""),
                    "currency": info.get("currency", "USD"),
                    "exchange": info.get("exchange", ""),
                    "fifty_two_week_high": info.get("fiftyTwoWeekHigh", 0),
                    "fifty_two_week_low": info.get("fiftyTwoWeekLow", 0),
                    "dividend_yield": info.get("dividendYield", 0),
                    "beta": info.get("beta", 0),
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
                    "company_name": company_name or symbol,
                    "market_cap": market_cap or 0,
                    "last_updated": datetime.now().isoformat(),
                }

        except Exception as e:
            logger.error(f"Error fetching stock info for {symbol}: {e}")
            return None

    async def get_market_hours(self, symbol: str) -> Dict:
        """Get market hours information for a symbol."""
        try:
            # Try Polygon API first
            market_status = self.polygon_client.get_market_status()
            return {
                "is_market_open": market_status.market == "open",
                "next_open": market_status.next_open,
                "next_close": market_status.next_close,
            }
        except Exception as e:
            logger.warning(f"Could not fetch market hours from Polygon: {e}")
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


stock_service = StockMarketService(
    os.getenv("POLYGON_API_KEY")
)  # Use POLYGON_API_KEY from environment variable


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

        # Create user with enhanced security and tracking
        mongo.users.insert_one(
            {
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
        )

        flash("Registration successful")
        return redirect(url_for("login"))

    return render_template("register.html")


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
@async_route  # Add this decorator
async def dashboard():
    user_data = mongo.users.find_one({"_id": ObjectId(current_user.get_id())})
    portfolio = user_data.get("portfolio", {})
    balance = Decimal(str(user_data.get("balance", "0")))

    portfolio_data = []
    total_portfolio_value = Decimal("0")

    # Fetch all stock prices concurrently
    async def fetch_all_prices():
        tasks = []
        for symbol in portfolio.keys():
            if portfolio[symbol]["shares"] > 0:
                tasks.append(stock_service.get_stock_price(symbol))
        return await asyncio.gather(*tasks)

    stock_prices = await fetch_all_prices()

    for (symbol, position), current_price in zip(portfolio.items(), stock_prices):
        if position["shares"] > 0 and current_price:
            shares = Decimal(str(position["shares"]))
            avg_price = Decimal(str(position["avg_price"]))
            current_price = Decimal(str(current_price))

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
                    "day_change": position.get("day_change", Decimal("0")),
                    "last_updated": datetime.now(),
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
    socketio.run(app, debug=True, port=8080, host='0.0.0.0')
