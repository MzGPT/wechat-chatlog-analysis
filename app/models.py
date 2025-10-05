from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, JSON, ForeignKey
from sqlalchemy.orm import mapped_column, Mapped, relationship
from datetime import datetime
from .db import Base


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # talker id or room id
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str | None] = mapped_column(String, nullable=True)  # single/group
    is_chatroom: Mapped[bool] = mapped_column(Boolean, default=False)
    members: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    messages: Mapped[list[Message]] = relationship("Message", back_populates="chat")  # type: ignore


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # wxid
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    alias: Mapped[str | None] = mapped_column(String, nullable=True)
    rating: Mapped[int] = mapped_column(Integer, default=50)
    labels: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str | None] = mapped_column(String, ForeignKey("chats.id"), index=True)
    sender_id: Mapped[str | None] = mapped_column(String, index=True)
    sender_name: Mapped[str | None] = mapped_column(String, nullable=True)
    talker_name: Mapped[str | None] = mapped_column(String, nullable=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    direction: Mapped[str | None] = mapped_column(String)  # in/out
    type: Mapped[str | None] = mapped_column(String)  # text/image/file/voice/video/link/other
    content_text: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSON)
    tags: Mapped[dict | None] = mapped_column(JSON)  # array-like
    derived: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    importance_score: Mapped[int] = mapped_column(Integer, default=50)
    upvotes: Mapped[int] = mapped_column(Integer, default=0)
    downvotes: Mapped[int] = mapped_column(Integer, default=0)
    ai_suggestions: Mapped[dict | None] = mapped_column(JSON)
    send_status: Mapped[str | None] = mapped_column(String)  # pending/sent/failed

    chat: Mapped[Chat | None] = relationship("Chat", back_populates="messages")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String)  # ai_reply/summary/send
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="pending")
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String)
    time_range: Mapped[str | None] = mapped_column(String)
    filters: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="pending")
    result_type: Mapped[str | None] = mapped_column(String)  # html/markdown/json
    result_body: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    artifacts: Mapped[list["ReportArtifact"]] = relationship(
        "ReportArtifact",
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ReportArtifact.sequence",
    )


class AnalysisSnapshot(Base):
    __tablename__ = "analysis_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    filters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    options: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    message_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    messages: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    contact_ratings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="ready")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    time_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    time_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(Integer, ForeignKey("messages.id"), index=True)
    kind: Mapped[str] = mapped_column(String)  # 约/问/答/顶/踩
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class InteractionExt(Base):
    __tablename__ = "interactions_ext"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SyncState(Base):
    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReportArtifact(Base):
    __tablename__ = "report_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id"), index=True)
    module: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    data_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    data_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    report: Mapped[Report] = relationship("Report", back_populates="artifacts")


# ===============
# New: Mail & Ext Adapters
# ===============

class EmailAccount(Base):
    """Outgoing/incoming mail account configuration.

    Note: Credentials are stored in JSON for flexibility (username/password/oauth).
    In production, consider encrypting the password at rest and masking in APIs.
    """

    __tablename__ = "email_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))  # display name in UI
    email_address: Mapped[str] = mapped_column(String(255), index=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)  # gmail/qq/outlook/custom
    imap_host: Mapped[str] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    smtp_host: Mapped[str] = mapped_column(String(255))
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    smtp_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    auth: Mapped[dict] = mapped_column(JSON, default=dict)  # {username, password, oauth_token?}
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class EmailMessage(Base):
    """Persisted email headers and light body for listing/search.

    Attachments and full raw bodies are omitted for now to keep the DB light.
    """

    __tablename__ = "email_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("email_accounts.id"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)  # Message-ID/UID
    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    from_addr: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_addrs: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    cc_addrs: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    bcc_addrs: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    direction: Mapped[str] = mapped_column(String(8), default="in")  # in/out
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    flags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # seen/flagged/etc
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ExtAdapter(Base):
    """Configured external adapter (e.g., langbot adapters for telegram/qq/feishu)."""

    __tablename__ = "ext_adapters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # e.g., telegram, qq, feishu
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    source_type: Mapped[str] = mapped_column(String(32), default="langbot")
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g., {log_dir, api_base, token}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdapterMessage(Base):
    """Messages ingested from adapters' logs/APIs, displayed in extension tabs."""

    __tablename__ = "adapter_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    adapter_key: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sender: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    direction: Mapped[str] = mapped_column(String(8), default="in")  # in/out
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
