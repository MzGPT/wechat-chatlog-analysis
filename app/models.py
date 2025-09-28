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
