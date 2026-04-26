from __future__ import annotations

from pathlib import Path
from typing import Any
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import json
import uuid
import shutil
from .models import Base, Order, OrderItem, Product, User, EscrowConfirmation, Review, SellerProfile, Message
import jwt
import stripe
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .db import db_session, engine
from .models import Base, Order, OrderItem, Product, User
from .security import hash_password, verify_password
from .settings import settings
from .utils import money_fmt, slugify
from .models import Base, Order, OrderItem, Product, User, EscrowConfirmation, Review, SellerProfile


BASE_DIR = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 2


def _create_token(user_id: int, is_admin: bool) -> str:
    payload = {
        "sub": str(user_id),
        "is_admin": is_admin,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


def _set_token_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=JWT_EXPIRE_HOURS * 3600,
    )


def _clear_token_cookie(response: Response) -> None:
    response.delete_cookie("access_token")


def _get_token_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = _decode_token(token)
    if not payload:
        return None
    try:
        uid = int(payload["sub"])
    except (KeyError, ValueError):
        return None
    return db.get(User, uid)


class _LoginRequired(Exception):
    pass


def _require_login(request: Request, db: Session) -> User:
    user = _get_token_user(request, db)
    if user is None:
        raise _LoginRequired()
    return user


def _require_admin(request: Request, db: Session) -> User:
    user = _require_login(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403)
    return user


app = FastAPI(title=settings.app_name)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.cookie_secure,
)

# ---------------------------------------------------------------------------
# Upload directory & static file serving
# ---------------------------------------------------------------------------

UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ---------------------------------------------------------------------------

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["money_fmt"] = money_fmt


def get_db() -> Session:
    with db_session() as db:
        yield db


def _get_cart(session: dict[str, Any]) -> dict[str, int]:
    cart = session.get("cart")
    if not isinstance(cart, dict):
        cart = {}
    normalized: dict[str, int] = {}
    for k, v in cart.items():
        try:
            pid = str(int(k))
            qty = int(v)
        except Exception:
            continue
        if qty > 0:
            normalized[pid] = min(qty, 99)
    session["cart"] = normalized
    return normalized


def _cart_count(session: dict[str, Any]) -> int:
    cart = _get_cart(session)
    return sum(cart.values())


def _flash(session: dict[str, Any]) -> str | None:
    msg = session.pop("flash", None)
    return msg if isinstance(msg, str) else None


def _template_ctx(request: Request, db: Session, **extra: Any) -> dict[str, Any]:
    user = _get_token_user(request, db)

    unread_count = 0
    if user:
        unread_count = db.scalar(
            select(func.count(Message.id)).where(
                Message.recipient_id == user.id,
                Message.read == False,
            )
        ) or 0

    return {
        "request": request,
        "title": extra.pop("title", settings.app_name),
        "cart_count": _cart_count(request.session),
        "flash": _flash(request.session),
        "current_user": user,
        "unread_count": unread_count,
        **extra,
    }

@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)

    with db_session() as db:
        admin = db.scalar(select(User).where(User.email == settings.admin_email))
        if admin is None:
            admin = User(
                email=settings.admin_email,
                password_hash=hash_password(settings.admin_password),
                is_admin=True,
            )
            db.add(admin)
            db.commit()
        else:
            admin.password_hash = hash_password(settings.admin_password)
            admin.is_admin = True
            db.commit()

    if settings.stripe_enabled and settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key

# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------

@app.get("/styles.css")
def styles_css() -> FileResponse:
    return FileResponse(str(BASE_DIR / "styles.css"))


@app.get("/favicon.ico")
def favicon() -> Response:
    raise HTTPException(status_code=404)


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/index.html")


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.get("/index.html", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    products = list(db.scalars(
        select(Product).where(Product.active == True, Product.validated == True).limit(12)
    ))
    return templates.TemplateResponse(
        "index.html",
        _template_ctx(request, db, title="Listables - Home", products=products),
    )


@app.get("/search.html", response_class=HTMLResponse)
def search(request: Request, q: str | None = None, db: Session = Depends(get_db)) -> HTMLResponse:
    stmt = select(Product).where(Product.active == True, Product.validated == True)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Product.name.ilike(like) | Product.description.ilike(like) | Product.category.ilike(like))
    products = list(db.scalars(stmt.limit(60)))
    return templates.TemplateResponse(
        "search.html",
        _template_ctx(request, db, title="Search - Listables", products=products, q=q),
    )


@app.get("/product-details.html", response_class=HTMLResponse)
def product_details(request: Request, slug: str, db: Session = Depends(get_db)) -> HTMLResponse:
    product = db.scalar(select(Product).where(Product.slug == slug))
    if product is None:
        return RedirectResponse(url="/404.html")
    return templates.TemplateResponse(
        "product-details.html",
        _template_ctx(request, db, title=f"{product.name} - Listables", product=product),
    )


@app.get("/cart.html", response_class=HTMLResponse)
def cart_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    cart = _get_cart(request.session)
    ids = [int(pid) for pid in cart.keys()]
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids)))}

    items: list[dict[str, Any]] = []
    subtotal_minor = 0
    currencies: set[str] = set()
    for pid_str, qty in cart.items():
        pid = int(pid_str)
        p = products.get(pid)
        if not p:
            continue
        currencies.add(p.currency)
        line_total = p.price_minor * qty
        subtotal_minor += line_total
        items.append({"product": p, "qty": qty, "line_total_minor": line_total})

    shipping_minor = 0
    total_minor = subtotal_minor + shipping_minor
    currency = next(iter(currencies)) if len(currencies) == 1 else "EGP"
    multi_currency = len(currencies) > 1
    return templates.TemplateResponse(
        "cart.html",
        _template_ctx(
            request, db,
            title="Listables - Cart",
            items=items,
            subtotal_minor=subtotal_minor,
            total_minor=total_minor,
            currency=currency,
            multi_currency=multi_currency,
        ),
    )


@app.post("/cart/add")
def cart_add(
    request: Request,
    product_id: int = Form(...),
    qty: int = Form(1),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    p = db.get(Product, product_id)
    if p is None or not p.active:
        request.session["flash"] = "Product not found."
        return RedirectResponse(url="/index.html", status_code=303)
    cart = _get_cart(request.session)
    qty = max(1, min(int(qty), 99))
    cart[str(product_id)] = min(cart.get(str(product_id), 0) + qty, 99)
    request.session["cart"] = cart
    request.session["flash"] = "Added to cart."
    return RedirectResponse(url="/cart.html", status_code=303)


@app.post("/cart/update")
async def cart_update(request: Request) -> RedirectResponse:
    data = await request.form()
    cart = _get_cart(request.session)
    remove_product_id = data.get("remove_product_id")
    for k, v in data.items():
        if not k.startswith("qty_"):
            continue
        try:
            pid = int(k.removeprefix("qty_"))
            qty = int(v)
        except Exception:
            continue
        if qty <= 0:
            cart.pop(str(pid), None)
        else:
            cart[str(pid)] = min(max(qty, 1), 99)

    if remove_product_id:
        try:
            cart.pop(str(int(remove_product_id)), None)
        except Exception:
            pass
    request.session["cart"] = cart
    request.session["flash"] = "Cart updated."
    return RedirectResponse(url="/cart.html", status_code=303)


# ---------------------------------------------------------------------------
# Checkout & Orders
# ---------------------------------------------------------------------------

@app.get("/checkout.html", response_class=HTMLResponse)
def checkout_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        _require_login(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to checkout."
        return RedirectResponse(url="/login.html", status_code=303)

    cart = _get_cart(request.session)
    ids = [int(pid) for pid in cart.keys()]
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids)))}

    items: list[dict[str, Any]] = []
    subtotal_minor = 0
    currencies: set[str] = set()
    for pid_str, qty in cart.items():
        pid = int(pid_str)
        p = products.get(pid)
        if not p:
            continue
        currencies.add(p.currency)
        line_total = p.price_minor * qty
        subtotal_minor += line_total
        items.append({"product": p, "qty": qty, "line_total_minor": line_total})

    shipping_minor = max(10000, int(subtotal_minor * 0.01))
    total_minor = subtotal_minor + shipping_minor
    currency = next(iter(currencies)) if len(currencies) == 1 else "EGP"
    multi_currency = len(currencies) > 1
    return templates.TemplateResponse(
        "checkout.html",
        _template_ctx(
            request, db,
            title="Checkout - Listables",
            items=items,
            subtotal_minor=subtotal_minor,
            shipping_minor=shipping_minor,
            total_minor=total_minor,
            currency=currency,
            multi_currency=multi_currency,
        ),
    )


@app.post("/checkout/place-order")
def place_order(
    request: Request,
    first_name: str = Form(...),
    company: str = Form(""),
    address_line1: str = Form(...),
    address_line2: str = Form(""),
    city: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    payment_method: str = Form("cod"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        _require_login(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to place an order."
        return RedirectResponse(url="/login.html", status_code=303)

    cart = _get_cart(request.session)
    if not cart:
        request.session["flash"] = "Your cart is empty."
        return RedirectResponse(url="/cart.html", status_code=303)

    ids = [int(pid) for pid in cart.keys()]
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids)))}
    items: list[OrderItem] = []
    subtotal_minor = 0
    currencies: set[str] = set()
    for pid_str, qty in cart.items():
        pid = int(pid_str)
        p = products.get(pid)
        if not p:
            continue
        currencies.add(p.currency)
        line_total = p.price_minor * qty
        subtotal_minor += line_total
        items.append(
            OrderItem(
                product_id=p.id,
                product_name=p.name,
                image_url=p.image_url,
                unit_price_minor=p.price_minor,
                qty=qty,
                line_total_minor=line_total,
            )
        )
    if not items:
        request.session["flash"] = "Your cart is empty."
        return RedirectResponse(url="/cart.html", status_code=303)

    if len(currencies) != 1:
        request.session["flash"] = "Your cart has multiple currencies. Please checkout items in one currency at a time."
        return RedirectResponse(url="/cart.html", status_code=303)
    currency = next(iter(currencies))

    shipping_minor = max(10000, int(subtotal_minor * 0.01))
    total_minor = subtotal_minor + shipping_minor
    user = _get_token_user(request, db)

    order = Order(
        user_id=user.id if user else None,
        customer_first_name=first_name,
        customer_email=email,
        customer_phone=phone,
        address_line1=address_line1,
        address_line2=address_line2,
        city=city,
        company=company,
        currency=currency,
        subtotal_minor=subtotal_minor,
        shipping_minor=shipping_minor,
        total_minor=total_minor,
        status="pending",
    )
    order.items = items
    db.add(order)
    db.commit()
    db.refresh(order)

    request.session["cart"] = {}
    return RedirectResponse(url=f"/order-confirmed.html?order_id={order.id}", status_code=303)


@app.get("/order-confirmed.html", response_class=HTMLResponse)
def order_confirmed(request: Request, order_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        _require_login(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to view your order."
        return RedirectResponse(url="/login.html", status_code=303)

    order = db.get(Order, order_id)
    if order is None:
        return RedirectResponse(url="/404.html")

    if (
        order.status != "paid"
        and settings.stripe_enabled
        and settings.stripe_secret_key
        and order.stripe_checkout_session_id
    ):
        try:
            stripe.api_key = settings.stripe_secret_key
            session = stripe.checkout.Session.retrieve(order.stripe_checkout_session_id)
            if session and session.get("payment_status") == "paid":
                order.status = "paid"
                db.commit()
                request.session["cart"] = {}
        except Exception:
            pass

    if order.status == "paid":
        request.session["cart"] = {}
    return templates.TemplateResponse(
        "order-confirmed.html",
        _template_ctx(request, db, title="Listables - Order Confirmed", order=order),
    )


@app.get("/orders.html", response_class=HTMLResponse)
def my_orders(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to view orders."
        return RedirectResponse(url="/login.html", status_code=303)

    orders = list(db.scalars(
        select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(200)
    ))

    orders_with_seller = []
    for o in orders:
        seller_id = None
        seller_product_id = None
        for item in o.items:
            if item.product_id:
                product = db.get(Product, item.product_id)
                if product and product.seller_id and product.seller_id != user.id:
                    seller_id = product.seller_id
                    seller_product_id = item.product_id
                    break
        orders_with_seller.append({
            "order": o,
            "seller_id": seller_id,
            "seller_product_id": seller_product_id,
        })

    return templates.TemplateResponse(
        "orders.html",
        _template_ctx(request, db, title="My Orders - Listables", orders_with_seller=orders_with_seller),
    )


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        _require_admin(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to access the admin panel."
        return RedirectResponse(url="/login.html", status_code=303)

    orders = list(db.scalars(select(Order).order_by(Order.id.desc()).limit(200)))
    orders_data = []
    for o in orders:
        seller_conf = db.scalar(
            select(EscrowConfirmation).where(
                EscrowConfirmation.order_id == o.id,
                EscrowConfirmation.role == "seller",
            )
        )
        buyer_conf = db.scalar(
            select(EscrowConfirmation).where(
                EscrowConfirmation.order_id == o.id,
                EscrowConfirmation.role == "buyer",
            )
        )
        orders_data.append({
            "order": o,
            "seller_confirmed": seller_conf and seller_conf.confirmed,
            "buyer_confirmed": buyer_conf and buyer_conf.confirmed,
            "seller_proof": seller_conf.image_url if seller_conf else None,
            "buyer_proof": buyer_conf.image_url if buyer_conf else None,
            "items_json": json.dumps([
                {
                    "product_name": item.product_name,
                    "qty": item.qty,
                    "image_url": item.image_url or "",
                    "line_total_fmt": money_fmt(o.currency, item.line_total_minor),
                }
                for item in o.items
            ])
        })
    return templates.TemplateResponse(
        "admin-dashboard.html",
        _template_ctx(request, db, title="Admin - All Orders", orders_data=orders_data),
    )

@app.get("/my-account.html", response_class=HTMLResponse)
def my_account_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to view your account."
        return RedirectResponse(url="/login.html", status_code=303)
    return templates.TemplateResponse(
        "my-account.html",
        _template_ctx(request, db, title="My Account - Listables"),
    )


@app.post("/account/update")
async def account_update(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)

    form = await request.form()
    first_name       = form.get("first_name", "").strip()
    last_name        = form.get("last_name", "").strip()
    email            = form.get("email", "").strip().lower()
    current_password = form.get("current_password", "")
    new_password     = form.get("new_password", "")
    confirm_password = form.get("confirm_password", "")

    # Update name and email
    if first_name:
        user.first_name = first_name
    if last_name:
        user.last_name = last_name
    if email and email != user.email:
        existing = db.scalar(select(User).where(User.email == email))
        if existing:
            request.session["flash"] = "That email is already in use."
            return RedirectResponse(url="/my-account.html", status_code=303)
        user.email = email

    # Update password only if fields are filled
    if current_password or new_password or confirm_password:
        if not verify_password(current_password, user.password_hash):
            request.session["flash"] = "Current password is incorrect."
            return RedirectResponse(url="/my-account.html", status_code=303)
        if new_password != confirm_password:
            request.session["flash"] = "New passwords do not match."
            return RedirectResponse(url="/my-account.html", status_code=303)
        if len(new_password) < 6:
            request.session["flash"] = "New password must be at least 6 characters."
            return RedirectResponse(url="/my-account.html", status_code=303)
        user.password_hash = hash_password(new_password)

    db.commit()
    request.session["flash"] = "Profile updated successfully."
    return RedirectResponse(url="/my-account.html", status_code=303)


@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        _require_admin(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to access the admin panel."
        return RedirectResponse(url="/login.html", status_code=303)

    orders = list(db.scalars(select(Order).order_by(Order.id.desc()).limit(200)))
    orders_data = []
    for o in orders:
        seller_conf = db.scalar(
            select(EscrowConfirmation).where(
                EscrowConfirmation.order_id == o.id,
                EscrowConfirmation.role == "seller",
            )
        )
        buyer_conf = db.scalar(
            select(EscrowConfirmation).where(
                EscrowConfirmation.order_id == o.id,
                EscrowConfirmation.role == "buyer",
            )
        )
        orders_data.append({
            "order": o,
            "seller_confirmed": seller_conf and seller_conf.confirmed,
            "buyer_confirmed": buyer_conf and buyer_conf.confirmed,
            "seller_proof": seller_conf.image_url if seller_conf else None,
            "buyer_proof": buyer_conf.image_url if buyer_conf else None,
            "items_json": json.dumps([
                {
                    "product_name": item.product_name,
                    "qty": item.qty,
                    "image_url": item.image_url or "",
                    "line_total_fmt": money_fmt(o.currency, item.line_total_minor),
                }
                for item in o.items
            ])
        })
    return templates.TemplateResponse(
        "admin-dashboard.html",
        _template_ctx(request, db, title="Admin - All Orders", orders_data=orders_data),
    )

@app.get("/admin-dashboard.html", response_class=HTMLResponse)
def admin_dashboard_alias() -> RedirectResponse:
    return RedirectResponse(url="/admin/dashboard")


@app.post("/admin/orders/{order_id}/delete")
def admin_delete_order(order_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        _require_admin(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)
    order = db.get(Order, order_id)
    if order:
        db.delete(order)
        db.commit()
    request.session["flash"] = f"Order #{order_id} deleted."
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/orders/{order_id}/edit")
async def admin_edit_order(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        _require_admin(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)

    form = await request.form()
    order = db.get(Order, order_id)
    if not order:
        request.session["flash"] = f"Order #{order_id} not found."
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    if form.get("status"):
        order.status = form["status"]
    if form.get("name"):
        order.customer_first_name = form["name"]
    if form.get("email"):
        order.customer_email = form["email"]
    if form.get("phone"):
        order.customer_phone = form["phone"]
    if form.get("addr1") is not None:
        order.address_line1 = form["addr1"]
    if form.get("addr2") is not None:
        order.address_line2 = form["addr2"]
    if form.get("city") is not None:
        order.city = form["city"]

    db.commit()
    request.session["flash"] = f"Order #{order_id} updated."
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.get("/admin/validate-listings", response_class=HTMLResponse)
def admin_validate_listings(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        _require_admin(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to access the admin panel."
        return RedirectResponse(url="/login.html", status_code=303)

    pending_listings = list(db.scalars(
        select(Product)
        .where(Product.seller_id != None, Product.validated == False, Product.active == False)
        .order_by(Product.created_at.asc())
    ))
    reviewed_listings = list(db.scalars(
        select(Product)
        .where(Product.seller_id != None, Product.validated == True)
        .order_by(Product.created_at.desc())
        .limit(30)
    ))
    one_week_ago = datetime.utcnow() - timedelta(days=7)
    approved_count = db.scalar(
        select(func.count(Product.id)).where(
            Product.seller_id != None,
            Product.validated == True,
            Product.active == True,
            Product.created_at >= one_week_ago,
        )
    ) or 0
    rejected_count = db.scalar(
        select(func.count(Product.id)).where(
            Product.seller_id != None,
            Product.validated == True,
            Product.active == False,
        )
    ) or 0

    return templates.TemplateResponse(
        "admin-validate-listings.html",
        _template_ctx(
            request, db,
            title="Validate Listings - Admin",
            pending_listings=pending_listings,
            reviewed_listings=reviewed_listings,
            approved_count=approved_count,
            rejected_count=rejected_count,
        ),
    )


@app.post("/admin/listings/{product_id}/approve")
def admin_approve_listing(product_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        _require_admin(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)
    product = db.get(Product, product_id)
    if product:
        product.active = True
        product.validated = True
        db.commit()
        request.session["flash"] = f"'{product.name}' is now live."
    return RedirectResponse(url="/admin/validate-listings", status_code=303)


@app.post("/admin/listings/{product_id}/reject")
def admin_reject_listing(product_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        _require_admin(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)
    product = db.get(Product, product_id)
    if product:
        product.active = False
        product.validated = True
        db.commit()
        request.session["flash"] = f"'{product.name}' has been rejected."
    return RedirectResponse(url="/admin/validate-listings", status_code=303)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.get("/login.html", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse("login.html", _template_ctx(request, db, title="Login - Listables"))


@app.get("/create-account.html", response_class=HTMLResponse)
def register_page(request: Request, role: str = "buyer", db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        "create-account.html",
        _template_ctx(request, db, title="Create account - Listables", preselect_role=role),
    )


@app.post("/auth/register")
def register(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("buyer"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    email = email.strip().lower()
    if len(password) < 6:
        request.session["flash"] = "Password must be at least 6 characters."
        return RedirectResponse(url="/create-account.html", status_code=303)
    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        request.session["flash"] = "Email already registered."
        return RedirectResponse(url="/login.html", status_code=303)
    is_seller = role == "seller"
    u = User(
        email=email,
        password_hash=hash_password(password),
        is_admin=False,
        is_seller=is_seller,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    token = _create_token(u.id, u.is_admin)
    response = RedirectResponse(url="/index.html", status_code=303)
    _set_token_cookie(response, token)
    request.session["flash"] = "Account created."
    return response


@app.post("/auth/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    email = email.strip().lower()
    print(f"[LOGIN] email={repr(email)}")
    u = db.scalar(select(User).where(User.email == email))
    if not u or not verify_password(password, u.password_hash):
        print(f"[LOGIN] FAILED for {repr(email)}")
        request.session["flash"] = "Invalid email or password."
        return RedirectResponse(url="/login.html", status_code=303)
    token = _create_token(u.id, u.is_admin)
    redirect_url = "/admin/dashboard" if u.is_admin else "/index.html"
    response = RedirectResponse(url=redirect_url, status_code=303)
    _set_token_cookie(response, token)
    request.session["flash"] = "Logged in."
    print(f"[LOGIN] SUCCESS id={u.id} is_admin={u.is_admin}")
    return response


@app.post("/auth/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.pop("user_id", None)
    request.session["flash"] = "Logged out."
    response = RedirectResponse(url="/index.html", status_code=303)
    _clear_token_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Seller
# ---------------------------------------------------------------------------

@app.get("/add-listings.html", response_class=HTMLResponse)
def add_listing_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to add a listing."
        return RedirectResponse(url="/login.html", status_code=303)
    if not user.is_seller:
        return RedirectResponse(url="/index.html", status_code=303)
    return templates.TemplateResponse(
        "add-listings.html",
        _template_ctx(request, db, title="Add Listing - Listables"),
    )


@app.get("/my-listings.html", response_class=HTMLResponse)
def my_listings(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to view your listings."
        return RedirectResponse(url="/login.html", status_code=303)
    if not user.is_seller:
        return RedirectResponse(url="/index.html", status_code=303)
    listings = list(db.scalars(select(Product).where(Product.seller_id == user.id).order_by(Product.id.desc())))
    return templates.TemplateResponse(
        "my-listings.html",
        _template_ctx(request, db, title="My Listings - Listables", listings=listings),
    )


@app.post("/seller/listings/add")
async def seller_add_listing(
    request: Request,
    name: str = Form(...),
    description: str = Form(...),
    price: float = Form(...),
    category: str = Form(...),
    stock_qty: int = Form(...),
    image_file: UploadFile = File(...),
    brand: str = Form(""),
    model: str = Form(""),
    condition: str = Form(""),
    warranty: str = Form(""),
    platform: str = Form(""),
    genre: str = Form(""),
    age_rating: str = Form(""),
    material: str = Form(""),
    color: str = Form(""),
    dimensions: str = Form(""),
    room_type: str = Form(""),
    assembly_required: str = Form(""),
    size: str = Form(""),
    gender: str = Form(""),
    sport: str = Form(""),
    use_type: str = Form(""),
    extra_notes: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)
    if not user.is_seller:
        return RedirectResponse(url="/index.html", status_code=303)

    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if image_file.content_type not in allowed_types:
        request.session["flash"] = "Invalid image type. Please upload JPG, PNG, WEBP or GIF."
        return RedirectResponse(url="/add-listings.html", status_code=303)

    ext_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    ext = ext_map.get(image_file.content_type, ".jpg")

    category_slug = category.lower().replace(" ", "-")
    unique_id = uuid.uuid4().hex[:12]
    filename = f"{category_slug}_{user.id}_{unique_id}{ext}"
    save_path = UPLOAD_DIR / filename

    with save_path.open("wb") as f:
        shutil.copyfileobj(image_file.file, f)

    image_url = f"/static/uploads/{filename}"

    extra_info: dict = {}
    if brand:             extra_info["Brand"] = brand
    if model:             extra_info["Model"] = model
    if condition:         extra_info["Condition"] = condition
    if warranty:          extra_info["Warranty"] = warranty
    if platform:          extra_info["Platform"] = platform
    if genre:             extra_info["Genre"] = genre
    if age_rating:        extra_info["Age Rating"] = age_rating
    if material:          extra_info["Material"] = material
    if color:             extra_info["Color"] = color
    if dimensions:        extra_info["Dimensions"] = dimensions
    if room_type:         extra_info["Room Type"] = room_type
    if assembly_required: extra_info["Assembly Required"] = assembly_required
    if size:              extra_info["Size"] = size
    if gender:            extra_info["Gender"] = gender
    if sport:             extra_info["Sport"] = sport
    if use_type:          extra_info["Use Type"] = use_type
    if extra_notes:       extra_info["Notes"] = extra_notes

    if extra_info:
        extras_text = "\n\n— Additional details —\n" + "\n".join(
            f"{k}: {v}" for k, v in extra_info.items()
        )
        description = description + extras_text

    price_minor = int(round(price * 100))
    product = Product(
        name=name,
        slug=slugify(name) + f"-{user.id}",
        description=description,
        category=category,
        image_url=image_url,
        currency="EGP",
        price_minor=price_minor,
        stock_qty=stock_qty,
        active=False,
        validated=False,
        seller_id=user.id,
    )
    db.add(product)
    db.commit()
    request.session["flash"] = "Listing submitted for review! It will go live once approved by an admin."
    return RedirectResponse(url="/my-listings.html", status_code=303)


@app.post("/seller/listings/{product_id}/delete")
def seller_delete_listing(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)
    product = db.get(Product, product_id)
    if product and product.seller_id == user.id:
        db.delete(product)
        db.commit()
        request.session["flash"] = "Listing deleted."
    return RedirectResponse(url="/my-listings.html", status_code=303)


# ---------------------------------------------------------------------------
# Seller — orders  ← MUST come before /seller/{seller_id}
# ---------------------------------------------------------------------------

@app.get("/seller/orders", response_class=HTMLResponse)
def seller_orders(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to view your orders."
        return RedirectResponse(url="/login.html", status_code=303)
    if not user.is_seller:
        return RedirectResponse(url="/index.html", status_code=303)

    seller_product_ids = [
        p.id for p in db.scalars(select(Product).where(Product.seller_id == user.id))
    ]

    if not seller_product_ids:
        seller_orders_list = []
    else:
        order_ids = [
            row.order_id for row in db.execute(
                select(OrderItem.order_id)
                .where(OrderItem.product_id.in_(seller_product_ids))
                .distinct()
            )
        ]
        seller_orders_list = list(db.scalars(
            select(Order).where(Order.id.in_(order_ids)).order_by(Order.id.desc())
        ))

    orders_data = []
    for o in seller_orders_list:
        seller_conf = db.scalar(
            select(EscrowConfirmation).where(
                EscrowConfirmation.order_id == o.id,
                EscrowConfirmation.role == "seller",
            )
        )
        buyer_conf = db.scalar(
            select(EscrowConfirmation).where(
                EscrowConfirmation.order_id == o.id,
                EscrowConfirmation.role == "buyer",
            )
        )
        orders_data.append({
            "order": o,
            "seller_confirmed": seller_conf and seller_conf.confirmed,
            "buyer_confirmed": buyer_conf and buyer_conf.confirmed,
        })

    return templates.TemplateResponse(
        "seller-orders.html",
        _template_ctx(request, db, title="My Sales - Listables", orders_data=orders_data),
    )


# ---------------------------------------------------------------------------
# Seller profile  ← MUST come after /seller/orders
# ---------------------------------------------------------------------------

@app.get("/seller/setup", response_class=HTMLResponse)
def seller_setup_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)
    if not user.is_seller:
        return RedirectResponse(url="/index.html", status_code=303)

    profile = db.scalar(select(SellerProfile).where(SellerProfile.user_id == user.id))
    return templates.TemplateResponse(
        "seller-setup.html",
        _template_ctx(request, db, title="Setup Your Shop", profile=profile),
    )


@app.post("/seller/setup")
async def seller_setup_submit(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)

    form = await request.form()
    shop_name = form.get("shop_name", "").strip()
    bio       = form.get("bio", "").strip()
    location  = form.get("location", "").strip()
    phone     = form.get("phone", "").strip()

    avatar_file = form.get("avatar_file")
    avatar_url  = None
    if avatar_file and hasattr(avatar_file, "content_type") and avatar_file.content_type in {"image/jpeg", "image/png", "image/webp"}:
        ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
        ext = ext_map[avatar_file.content_type]
        filename = f"avatar_{user.id}_{uuid.uuid4().hex[:8]}{ext}"
        save_path = UPLOAD_DIR / filename
        with save_path.open("wb") as f:
            shutil.copyfileobj(avatar_file.file, f)
        avatar_url = f"/static/uploads/{filename}"

    profile = db.scalar(select(SellerProfile).where(SellerProfile.user_id == user.id))
    if profile:
        profile.shop_name = shop_name
        profile.bio       = bio
        profile.location  = location
        profile.phone     = phone
        if avatar_url:
            profile.avatar_url = avatar_url
    else:
        profile = SellerProfile(
            user_id=user.id,
            shop_name=shop_name,
            bio=bio,
            location=location,
            phone=phone,
            avatar_url=avatar_url,
        )
        db.add(profile)

    db.commit()
    request.session["flash"] = "Shop profile saved."
    return RedirectResponse(url=f"/seller/{user.id}", status_code=303)


@app.get("/seller/{seller_id}", response_class=HTMLResponse)
def seller_profile_page(seller_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    seller = db.get(User, seller_id)
    if not seller or not seller.is_seller:
        raise HTTPException(status_code=404)

    profile  = db.scalar(select(SellerProfile).where(SellerProfile.user_id == seller_id))
    products = list(db.scalars(
        select(Product).where(Product.seller_id == seller_id, Product.active == True, Product.validated == True)
    ))
    reviews  = list(db.scalars(
        select(Review).where(Review.seller_id == seller_id).order_by(Review.created_at.desc())
    ))

    avg_rating = round(sum(r.rating for r in reviews) / len(reviews), 1) if reviews else None

    return templates.TemplateResponse(
        "seller-profile.html",
        _template_ctx(
            request, db,
            title=f"{profile.shop_name if profile else seller.email} - Seller",
            seller=seller,
            profile=profile,
            products=products,
            reviews=reviews,
            avg_rating=avg_rating,
        ),
    )


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------

@app.get("/contact.html", response_class=HTMLResponse)
def contact_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        "contact.html",
        _template_ctx(request, db, title="Contact - Listables"),
    )


@app.post("/contact/send")
async def contact_send(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    name = form.get("name", "")
    email = form.get("email", "")
    phone = form.get("phone", "")
    message = form.get("message", "")

    if not name or not email or not message:
        request.session["flash"] = "Please fill in all required fields."
        return RedirectResponse(url="/contact.html", status_code=303)

    if settings.contact_email_from and settings.contact_email_password and settings.contact_email_to:
        try:
            msg = MIMEMultipart()
            msg["From"] = settings.contact_email_from
            msg["To"] = settings.contact_email_to
            msg["Subject"] = f"New Contact Message from {name}"
            body = f"""
New contact form submission:

Name:    {name}
Email:   {email}
Phone:   {phone}
Message:
{message}
            """
            msg.attach(MIMEText(body, "plain"))
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(settings.contact_email_from, settings.contact_email_password)
                server.sendmail(settings.contact_email_from, settings.contact_email_to, msg.as_string())
        except Exception as e:
            print(f"Email error: {e}")

    request.session["flash"] = f"Thanks {name}! Your message has been received. We'll get back to you within 24 hours."
    return RedirectResponse(url="/contact.html", status_code=303)




# ---------------------------------------------------------------------------
# Wishlist
# ---------------------------------------------------------------------------

@app.get("/wishlist.html", response_class=HTMLResponse)
def wishlist_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    wishlist_ids = request.session.get("wishlist", [])
    products = []
    if wishlist_ids:
        products = list(db.scalars(
            select(Product).where(Product.id.in_(wishlist_ids), Product.active == True, Product.validated == True)
        ))
    return templates.TemplateResponse(
        "wishlist.html",
        _template_ctx(request, db, title="Wishlist - Listables", products=products),
    )


@app.post("/wishlist/add")
def wishlist_add(
    request: Request,
    product_id: int = Form(...),
) -> RedirectResponse:
    wishlist = request.session.get("wishlist", [])
    if product_id not in wishlist:
        wishlist.append(product_id)
    request.session["wishlist"] = wishlist
    request.session["flash"] = "Added to wishlist."
    return RedirectResponse(url="/wishlist.html", status_code=303)


@app.post("/wishlist/remove")
def wishlist_remove(
    request: Request,
    product_id: int = Form(...),
) -> RedirectResponse:
    wishlist = request.session.get("wishlist", [])
    wishlist = [i for i in wishlist if i != product_id]
    request.session["wishlist"] = wishlist
    request.session["flash"] = "Removed from wishlist."
    return RedirectResponse(url="/wishlist.html", status_code=303)


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------

_CATEGORY_FILTER_FIELD: dict[str, str] = {
    "gaming":      "Platform",
    "game":        "Platform",
    "electronics": "Brand",
    "home":        "Material",
    "furniture":   "Material",
    "fashion":     "Gender",
    "clothing":    "Gender",
    "sport":       "Sport",
    "sports":      "Sport",
}


def _filter_field_for(category_name: str) -> str:
    lower = category_name.lower()
    for kw, field in _CATEGORY_FILTER_FIELD.items():
        if kw in lower:
            return field
    return "Genre"


def _extract_detail(description: str, field: str) -> str | None:
    if "— Additional details —" not in description:
        return None
    extras_raw = description.split("— Additional details —", 1)[1]
    for line in extras_raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            if k.strip().lower() == field.lower():
                return v.strip()
    return None


@app.get("/category/{category_name}", response_class=HTMLResponse)
def category_page(
    category_name: str,
    request: Request,
    q: str | None = None,
    filter_type: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:

    stmt = (
        select(Product)
        .where(
            Product.active == True,
            Product.validated == True,
            Product.category.ilike(f"%{category_name}%"),
        )
    )

    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            Product.name.ilike(like) | Product.description.ilike(like)
        )

    products = list(db.scalars(stmt.limit(120)))

    filter_field = _filter_field_for(category_name)
    filter_options: list[str] = []
    seen: set[str] = set()
    for p in products:
        val = _extract_detail(p.description, filter_field)
        if val and val not in seen:
            seen.add(val)
            filter_options.append(val)
    filter_options.sort()

    if filter_type and filter_type.strip():
        products = [
            p for p in products
            if _extract_detail(p.description, filter_field) == filter_type
        ]

    return templates.TemplateResponse(
        "category.html",
        _template_ctx(
            request, db,
            title=f"{category_name} - Listables",
            products=products,
            category_name=category_name,
            q=q,
            filter_type=filter_type,
            filter_options=filter_options,
        ),
    )


# ---------------------------------------------------------------------------
# About & fallback
# ---------------------------------------------------------------------------

@app.get("/about.html", response_class=HTMLResponse)
def about_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        "about.html",
        _template_ctx(request, db, title="About - Listables"),
    )


@app.get("/{page_name}.html")
def static_html(page_name: str) -> Response:
    allow = {
        "faq",
        "wishlist",
        "chat",
        "electronics",
        "admin-dashboard",
        "admin-listings",
        "admin-users",
        "add-product",
        "become-seller",
        "orders-selling",
        "404",
    }

    if page_name not in allow:
        raise HTTPException(status_code=404)
    path = BASE_DIR / f"{page_name}.html"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(str(path))

@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        _require_admin(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to access the admin panel."
        return RedirectResponse(url="/login.html", status_code=303)

    users = list(db.scalars(select(User).order_by(User.id.asc())))

    seller_sales = {}
    seller_orders_map = {}
    for u in users:
        if u.is_seller:
            product_ids = [
                p.id for p in db.scalars(select(Product).where(Product.seller_id == u.id))
            ]
            if product_ids:
                sales_count = db.scalar(
                    select(func.count(OrderItem.id)).where(OrderItem.product_id.in_(product_ids))
                ) or 0
                order_ids = [
                    row.order_id for row in db.execute(
                        select(OrderItem.order_id)
                        .where(OrderItem.product_id.in_(product_ids))
                        .distinct()
                    )
                ]
                seller_orders_map[u.id] = list(db.scalars(
                    select(Order).where(Order.id.in_(order_ids)).order_by(Order.id.desc())
                ))
            else:
                sales_count = 0
                seller_orders_map[u.id] = []
            seller_sales[u.id] = sales_count

    return templates.TemplateResponse(
        "admin-users.html",
        _template_ctx(
            request, db,
            title="Users - Admin",
            users=users,
            seller_sales=seller_sales,
            seller_orders_map=seller_orders_map,
        ),
    )

# ---------------------------------------------------------------------------
# Escrow confirmations
# ---------------------------------------------------------------------------

@app.get("/order/{order_id}/confirm", response_class=HTMLResponse)
def escrow_confirm_page(order_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)

    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404)

    role = None
    if order.user_id == user.id:
        role = "buyer"
    else:
        product_ids = [item.product_id for item in order.items]
        seller_products = db.scalars(
            select(Product).where(Product.id.in_(product_ids), Product.seller_id == user.id)
        ).all()
        if seller_products:
            role = "seller"

    if not role:
        raise HTTPException(status_code=403)

    confirmation = db.scalar(
        select(EscrowConfirmation).where(
            EscrowConfirmation.order_id == order_id,
            EscrowConfirmation.role == role,
        )
    )

    seller_conf = db.scalar(
        select(EscrowConfirmation).where(
            EscrowConfirmation.order_id == order_id,
            EscrowConfirmation.role == "seller",
        )
    )
    buyer_conf = db.scalar(
        select(EscrowConfirmation).where(
            EscrowConfirmation.order_id == order_id,
            EscrowConfirmation.role == "buyer",
        )
    )

    already_reviewed = db.scalar(
        select(Review).where(Review.order_id == order_id, Review.buyer_id == user.id)
    ) if role == "buyer" else None

    return templates.TemplateResponse(
        "escrow-confirm.html",
        _template_ctx(
            request, db,
            title=f"Confirm Order #{order_id}",
            order=order,
            role=role,
            confirmation=confirmation,
            seller_conf=seller_conf,
            buyer_conf=buyer_conf,
            already_reviewed=already_reviewed,
        ),
    )


@app.post("/order/{order_id}/confirm")
async def escrow_confirm_submit(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)

    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404)

    role = None
    if order.user_id == user.id:
        role = "buyer"
    else:
        product_ids = [item.product_id for item in order.items]
        seller_products = db.scalars(
            select(Product).where(Product.id.in_(product_ids), Product.seller_id == user.id)
        ).all()
        if seller_products:
            role = "seller"

    if not role:
        raise HTTPException(status_code=403)

    form = await request.form()
    image_file = form.get("image_file")

    image_url = None
    if image_file and hasattr(image_file, "content_type") and image_file.content_type in {"image/jpeg", "image/png", "image/webp"}:
        ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
        ext = ext_map[image_file.content_type]
        filename = f"confirm_{order_id}_{role}_{uuid.uuid4().hex[:8]}{ext}"
        save_path = UPLOAD_DIR / filename
        with save_path.open("wb") as f:
            shutil.copyfileobj(image_file.file, f)
        image_url = f"/static/uploads/{filename}"

    existing = db.scalar(
        select(EscrowConfirmation).where(
            EscrowConfirmation.order_id == order_id,
            EscrowConfirmation.role == role,
        )
    )
    if existing:
        existing.confirmed = True
        existing.image_url = image_url or existing.image_url
        existing.confirmed_at = datetime.utcnow()
    else:
        conf = EscrowConfirmation(
            order_id=order_id,
            role=role,
            confirmed=True,
            image_url=image_url,
            confirmed_at=datetime.utcnow(),
        )
        db.add(conf)

    db.commit()
    request.session["flash"] = "Confirmation submitted successfully."
    return RedirectResponse(url=f"/order/{order_id}/confirm", status_code=303)


@app.post("/admin/orders/{order_id}/complete")
def admin_complete_order(order_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        _require_admin(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)

    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404)

    order.status = "completed"
    db.commit()
    request.session["flash"] = f"Order #{order_id} marked as completed. Seller payout released."
    return RedirectResponse(url="/admin/dashboard", status_code=303)


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------

@app.post("/order/{order_id}/review")
async def submit_review(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)

    order = db.get(Order, order_id)
    if not order or order.user_id != user.id:
        raise HTTPException(status_code=403)

    if order.status != "completed":
        request.session["flash"] = "You can only review after the order is completed."
        return RedirectResponse(url=f"/order/{order_id}/confirm", status_code=303)

    existing = db.scalar(select(Review).where(Review.order_id == order_id, Review.buyer_id == user.id))
    if existing:
        request.session["flash"] = "You already reviewed this order."
        return RedirectResponse(url=f"/order/{order_id}/confirm", status_code=303)

    form = await request.form()
    rating = int(form.get("rating", 5))
    comment = form.get("comment", "").strip()

    product_ids = [item.product_id for item in order.items]
    seller_product = db.scalar(select(Product).where(Product.id.in_(product_ids), Product.seller_id != None))
    if not seller_product:
        request.session["flash"] = "Could not find seller for this order."
        return RedirectResponse(url=f"/order/{order_id}/confirm", status_code=303)

    review = Review(
        order_id=order_id,
        seller_id=seller_product.seller_id,
        buyer_id=user.id,
        rating=rating,
        comment=comment,
    )
    db.add(review)
    db.commit()
    request.session["flash"] = "Review submitted. Thank you!"
    return RedirectResponse(url=f"/order/{order_id}/confirm", status_code=303)


# ---------------------------------------------------------------------------
# Buyer cancel order
# ---------------------------------------------------------------------------

@app.post("/order/{order_id}/cancel")
def buyer_cancel_order(order_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)

    order = db.get(Order, order_id)
    if not order or order.user_id != user.id:
        raise HTTPException(status_code=403)

    if order.status == "completed":
        request.session["flash"] = "Completed orders cannot be cancelled."
        return RedirectResponse(url="/orders.html", status_code=303)

    order.status = "cancelled"
    db.commit()
    request.session["flash"] = f"Order #{order_id} has been cancelled."
    return RedirectResponse(url="/orders.html", status_code=303)

# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

@app.get("/messages/compose", response_class=HTMLResponse)
def compose_message_page(
    request: Request,
    seller_id: int,
    product_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        _require_login(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to message a seller."
        return RedirectResponse(url="/login.html", status_code=303)

    seller = db.get(User, seller_id)
    if not seller or not seller.is_seller:
        raise HTTPException(status_code=404)

    try:
        pid = int(product_id) if product_id else None
    except (ValueError, TypeError):
        pid = None
    product = db.get(Product, pid) if pid else None
    profile = db.scalar(select(SellerProfile).where(SellerProfile.user_id == seller_id))

    return templates.TemplateResponse(
        "compose-message.html",
        _template_ctx(
            request, db,
            title=f"Message {profile.shop_name if profile else seller.email}",
            seller=seller,
            profile=profile,
            product=product,
        ),
    )


@app.post("/messages/send")
async def send_message(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)

    form = await request.form()
    recipient_id = int(form.get("recipient_id", 0))
    body = form.get("body", "").strip()
    product_id_raw = form.get("product_id")
    try:
        product_id = int(product_id_raw) if product_id_raw else None
    except (ValueError, TypeError):
        product_id = None

    if not body:
        request.session["flash"] = "Message cannot be empty."
        return RedirectResponse(
            url=f"/messages/compose?seller_id={recipient_id}&product_id={product_id or ''}",
            status_code=303,
        )

    if user.id == recipient_id:
        request.session["flash"] = "You cannot message yourself."
        return RedirectResponse(url="/index.html", status_code=303)

    from .models import Message
    msg = Message(
        sender_id=user.id,
        recipient_id=recipient_id,
        product_id=product_id,
        body=body,
    )
    db.add(msg)
    db.commit()

    request.session["flash"] = "Message sent!"
    return RedirectResponse(url=f"/messages/inbox", status_code=303)


@app.get("/messages/inbox", response_class=HTMLResponse)
def inbox(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        request.session["flash"] = "Please login to view messages."
        return RedirectResponse(url="/login.html", status_code=303)

    from .models import Message
    received = list(db.scalars(
        select(Message)
        .where(Message.recipient_id == user.id)
        .order_by(Message.created_at.desc())
        .limit(100)
    ))
    sent = list(db.scalars(
        select(Message)
        .where(Message.sender_id == user.id)
        .order_by(Message.created_at.desc())
        .limit(100)
    ))

    # Mark received as read
    for m in received:
        m.read = True
    db.commit()

    return templates.TemplateResponse(
        "inbox.html",
        _template_ctx(request, db, title="Messages - Listables", received=received, sent=sent),
    )

@app.get("/messages/conversation", response_class=HTMLResponse)
def conversation_view(
    request: Request,
    with_user: int,
    product_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)

    other = db.get(User, with_user)
    if not other:
        raise HTTPException(status_code=404)

    profile = db.scalar(select(SellerProfile).where(SellerProfile.user_id == with_user))
    try:
        pid = int(product_id) if product_id else None
    except (ValueError, TypeError):
        pid = None
    product = db.get(Product, pid) if pid else None

    messages = list(db.scalars(
        select(Message).where(
            (
                (Message.sender_id == user.id) & (Message.recipient_id == with_user)
            ) | (
                (Message.sender_id == with_user) & (Message.recipient_id == user.id)
            )
        ).order_by(Message.created_at.asc())
    ))

    # Mark all received as read
    for m in messages:
        if m.recipient_id == user.id:
            m.read = True
    db.commit()

    return templates.TemplateResponse(
        "conversation.html",
        _template_ctx(
            request, db,
            title=f"Conversation with {profile.shop_name if profile else other.email}",
            other=other,
            profile=profile,
            product=product,
            messages=messages,
        ),
    )


@app.post("/messages/send-image")
async def send_image_message(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        user = _require_login(request, db)
    except _LoginRequired:
        return RedirectResponse(url="/login.html", status_code=303)

    form = await request.form()
    recipient_id = int(form.get("recipient_id", 0))
    product_id_raw = form.get("product_id")
    try:
        product_id = int(product_id_raw) if product_id_raw else None
    except (ValueError, TypeError):
        product_id = None
    body = form.get("body", "").strip() or ""

    image_file = form.get("image_file")
    image_url = None
    if image_file and hasattr(image_file, "content_type") and image_file.content_type in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
        ext = ext_map[image_file.content_type]
        filename = f"msg_{user.id}_{uuid.uuid4().hex[:10]}{ext}"
        save_path = UPLOAD_DIR / filename
        with save_path.open("wb") as f:
            shutil.copyfileobj(image_file.file, f)
        image_url = f"/static/uploads/{filename}"

    if not body and not image_url:
        request.session["flash"] = "Message cannot be empty."
        return RedirectResponse(url=f"/messages/conversation?with_user={recipient_id}&product_id={product_id or ''}", status_code=303)

    final_body = body
    if image_url:
        final_body = (body + f"\n[img]{image_url}[/img]").strip()

    msg = Message(
        sender_id=user.id,
        recipient_id=recipient_id,
        product_id=product_id,
        body=final_body,
    )
    db.add(msg)
    db.commit()

    return RedirectResponse(url=f"/messages/conversation?with_user={recipient_id}&product_id={product_id or ''}", status_code=303)