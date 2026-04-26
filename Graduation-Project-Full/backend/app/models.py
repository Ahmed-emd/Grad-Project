from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_seller: Mapped[bool] = mapped_column(Boolean, default=False)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)   # ← add
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)    # ← add
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    orders: Mapped[list["Order"]] = relationship(back_populates="user")
    products: Mapped[list["Product"]] = relationship(back_populates="seller")
    seller_profile: Mapped["SellerProfile | None"] = relationship(back_populates="user", uselist=False)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("slug", name="uq_products_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(220), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(100), default="General")
    currency: Mapped[str] = mapped_column(String(10), default="EGP")
    price_minor: Mapped[int] = mapped_column(Integer)
    stock_qty: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    seller: Mapped["User | None"] = relationship(back_populates="products")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    customer_first_name: Mapped[str] = mapped_column(String(100), default="")
    customer_email: Mapped[str] = mapped_column(String(320), default="")
    customer_phone: Mapped[str] = mapped_column(String(40), default="")
    address_line1: Mapped[str] = mapped_column(String(255), default="")
    address_line2: Mapped[str] = mapped_column(String(255), default="")
    city: Mapped[str] = mapped_column(String(120), default="")
    company: Mapped[str] = mapped_column(String(120), default="")

    currency: Mapped[str] = mapped_column(String(10), default="EGP")
    subtotal_minor: Mapped[int] = mapped_column(Integer, default=0)
    shipping_minor: Mapped[int] = mapped_column(Integer, default=0)
    total_minor: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(40), default="pending")

    stripe_checkout_session_id: Mapped[str] = mapped_column(String(255), default="")
    stripe_payment_intent_id: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    user: Mapped["User | None"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    confirmations: Mapped[list["EscrowConfirmation"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)

    product_name: Mapped[str] = mapped_column(String(200))
    image_url: Mapped[str] = mapped_column(Text, default="")
    unit_price_minor: Mapped[int] = mapped_column(Integer)
    qty: Mapped[int] = mapped_column(Integer)
    line_total_minor: Mapped[int] = mapped_column(Integer)

    order: Mapped["Order"] = relationship(back_populates="items")


class EscrowConfirmation(Base):
    __tablename__ = "escrow_confirmations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)   # "seller" or "buyer"
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    order: Mapped["Order"] = relationship(back_populates="confirmations")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)   # 1-5
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    order: Mapped["Order"] = relationship()
    seller: Mapped["User"] = relationship(foreign_keys=[seller_id])
    buyer: Mapped["User"] = relationship(foreign_keys=[buyer_id])


class SellerProfile(Base):
    __tablename__ = "seller_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    shop_name: Mapped[str] = mapped_column(String(200), nullable=False)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    user: Mapped["User"] = relationship(back_populates="seller_profile")

class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    sender: Mapped["User"] = relationship(foreign_keys=[sender_id])
    recipient: Mapped["User"] = relationship(foreign_keys=[recipient_id])
    product: Mapped["Product | None"] = relationship()