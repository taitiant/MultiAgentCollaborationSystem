from __future__ import annotations
import os
import json
import uuid
import time
from threading import Condition, Lock
from typing import List, Optional, Dict, Any
from sqlalchemy import create_engine, Column, String, Integer, Text, DateTime, Index, desc
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone

DB_URL = os.getenv("DB_URL") or "sqlite:///data/runtime.db"
os.makedirs(os.path.dirname(DB_URL.replace("sqlite:///", "")), exist_ok=True) if DB_URL.startswith("sqlite") else None

engine_kwargs = {"echo": False, "future": True}
if DB_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
engine = create_engine(DB_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

_TASK_UPDATE_LOCK = Lock()
_TASK_UPDATE_CONDITION = Condition(_TASK_UPDATE_LOCK)
_TASK_UPDATE_SNAPSHOTS: Dict[str, Dict[str, Any]] = {}


class TaskModel(Base):
    __tablename__ = "tasks"
    task_id = Column(String, primary_key=True)
    domain = Column(String)
    required_caps = Column(Text)
    context = Column(Text)
    priority = Column(Integer)
    status = Column(String)
    workspace_path = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_tasks_status_updated_at", "status", "updated_at"),
    )


class EventModel(Base):
    __tablename__ = "events"
    event_id = Column(String, primary_key=True)
    task_id = Column(String)
    actor_id = Column(String)
    event_type = Column(String)
    payload = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_events_task_timestamp", "task_id", "timestamp"),
        Index("ix_events_task_type_timestamp", "task_id", "event_type", "timestamp"),
    )


class ConversationMessageModel(Base):
    __tablename__ = "conversation_messages"
    message_id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False)
    stage_name = Column(String, nullable=True)
    stage_type = Column(String, nullable=True)
    conversation_id = Column(String, nullable=False)
    thread_kind = Column(String, nullable=False, default="stage_loop")
    thread_scope = Column(String, nullable=False, default="local")
    actor_id = Column(String, nullable=False)
    actor_role = Column(String, nullable=False, default="")
    recipient_id = Column(String, nullable=True)
    message_type = Column(String, nullable=False, default="comment")
    content = Column(Text, nullable=False, default="")
    payload = Column(Text, nullable=False, default="{}")
    turn_index = Column(Integer, nullable=False, default=1)
    reply_to = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_conversation_messages_task_stage_created", "task_id", "stage_name", "created_at"),
        Index("ix_conversation_messages_conversation_created", "conversation_id", "created_at"),
    )


class BlackboardEntryModel(Base):
    __tablename__ = "blackboard_entries"
    entry_id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False)
    stage_name = Column(String, nullable=True)
    entry_key = Column(String, nullable=False)
    entry_type = Column(String, nullable=False, default="note")
    title = Column(String, nullable=False, default="")
    content = Column(Text, nullable=False, default="")
    payload = Column(Text, nullable=False, default="{}")
    source_message_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_blackboard_entries_task_created", "task_id", "created_at"),
        Index("ix_blackboard_entries_task_key_updated", "task_id", "entry_key", "updated_at"),
    )


class AiCredentialModel(Base):
    __tablename__ = "ai_credentials"
    credential_id = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    base_url = Column(Text, nullable=False)
    api_key_env = Column(String, nullable=True)
    api_key = Column(Text, nullable=True)
    api_key_hint = Column(String, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_ai_credentials_name", "name"),
    )


class AiModelModel(Base):
    __tablename__ = "ai_models"
    model_id = Column(String, primary_key=True)
    credential_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    provider_type = Column(String, nullable=False, default="openai-compatible")
    model_kind = Column(String, nullable=False, default="llm")
    extra_config = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_ai_models_credential_kind", "credential_id", "model_kind"),
        Index("ix_ai_models_name", "name"),
    )


class AiStageBindingModel(Base):
    __tablename__ = "ai_stage_bindings"
    stage_name = Column(String, primary_key=True)
    model_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(engine)


def _now() -> datetime:
    return datetime.utcnow()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dt_to_timestamp(value: datetime | None) -> float | None:
    normalized = _as_utc(value)
    return normalized.timestamp() if normalized else None


def _dt_to_iso(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat().replace("+00:00", "Z") if normalized else None


def _default_task_update_snapshot(task_id: str) -> Dict[str, Any]:
    return {
        "task_id": str(task_id or ""),
        "version": 0,
        "task_version": 0,
        "events_version": 0,
        "collaboration_version": 0,
        "updated_at": 0.0,
    }


def get_task_update_snapshot(task_id: str) -> Dict[str, Any]:
    with _TASK_UPDATE_CONDITION:
        snapshot = dict(_TASK_UPDATE_SNAPSHOTS.get(task_id) or _default_task_update_snapshot(task_id))
    return snapshot


def notify_task_update(task_id: str, scopes: List[str] | tuple[str, ...] | set[str] | None = None) -> Dict[str, Any]:
    normalized_scopes = {str(scope or "").strip() for scope in (scopes or ["task"]) if str(scope or "").strip()}
    if not normalized_scopes:
        normalized_scopes = {"task"}
    with _TASK_UPDATE_CONDITION:
        snapshot = dict(_TASK_UPDATE_SNAPSHOTS.get(task_id) or _default_task_update_snapshot(task_id))
        snapshot["version"] = int(snapshot.get("version") or 0) + 1
        if "task" in normalized_scopes:
            snapshot["task_version"] = int(snapshot.get("task_version") or 0) + 1
        if "events" in normalized_scopes:
            snapshot["events_version"] = int(snapshot.get("events_version") or 0) + 1
        if "collaboration" in normalized_scopes:
            snapshot["collaboration_version"] = int(snapshot.get("collaboration_version") or 0) + 1
        snapshot["updated_at"] = time.time()
        _TASK_UPDATE_SNAPSHOTS[str(task_id or "")] = snapshot
        _TASK_UPDATE_CONDITION.notify_all()
        return dict(snapshot)


def wait_for_task_update(task_id: str, after_version: int = 0, timeout: float = 15.0) -> Optional[Dict[str, Any]]:
    task_key = str(task_id or "")
    end_at = time.time() + max(0.1, float(timeout or 0.1))
    with _TASK_UPDATE_CONDITION:
        while True:
            snapshot = _TASK_UPDATE_SNAPSHOTS.get(task_key) or _default_task_update_snapshot(task_key)
            if int(snapshot.get("version") or 0) > int(after_version or 0):
                return dict(snapshot)
            remaining = end_at - time.time()
            if remaining <= 0:
                return None
            _TASK_UPDATE_CONDITION.wait(timeout=remaining)


def _api_key_hint(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "*" * len(raw)
    return f"{raw[:4]}***{raw[-4:]}"


# ---- tasks/events ----
def save_task(task_id: str, domain: str, required_caps: list, context: dict, priority: int, workspace_path: str, status: str):
    with SessionLocal() as db:
        t = TaskModel(
            task_id=task_id,
            domain=domain,
            required_caps=json.dumps(required_caps, ensure_ascii=False),
            context=json.dumps(context, ensure_ascii=False),
            priority=priority,
            status=status,
            workspace_path=workspace_path,
        )
        db.merge(t)
        db.commit()
    notify_task_update(task_id, ["task"])


def update_task_status(task_id: str, status: str):
    with SessionLocal() as db:
        t = db.get(TaskModel, task_id)
        if t:
            t.status = status
            t.updated_at = _now()
            db.commit()
    notify_task_update(task_id, ["task"])


def update_task_context(task_id: str, context: dict):
    with SessionLocal() as db:
        t = db.get(TaskModel, task_id)
        if t:
            t.context = json.dumps(context, ensure_ascii=False)
            t.updated_at = _now()
            db.commit()
    notify_task_update(task_id, ["task"])


def list_tasks() -> List[dict]:
    with SessionLocal() as db:
        rows = db.query(TaskModel).order_by(desc(TaskModel.updated_at), desc(TaskModel.created_at)).all()
        return [
            {
                "task_id": r.task_id,
                "domain": r.domain,
                "required_capabilities": json.loads(r.required_caps or "[]"),
                "context": json.loads(r.context or "{}"),
                "priority": r.priority,
                "status": r.status,
                "workspace_path": r.workspace_path,
                "created_at": _dt_to_iso(r.created_at),
            }
            for r in rows
        ]


def get_task(task_id: str) -> Optional[dict]:
    with SessionLocal() as db:
        row = db.get(TaskModel, task_id)
        if not row:
            return None
        return {
            "task_id": row.task_id,
            "domain": row.domain,
            "required_capabilities": json.loads(row.required_caps or "[]"),
            "context": json.loads(row.context or "{}"),
            "priority": row.priority,
            "status": row.status,
            "workspace_path": row.workspace_path,
            "created_at": _dt_to_iso(row.created_at),
        }


def log_event(event_id: str, task_id: str, actor_id: str, event_type: str, payload: dict, timestamp: float):
    with SessionLocal() as db:
        evt = EventModel(
            event_id=event_id,
            task_id=task_id,
            actor_id=actor_id,
            event_type=event_type,
            payload=json.dumps(payload, ensure_ascii=False),
            timestamp=datetime.utcfromtimestamp(timestamp),
        )
        db.add(evt)
        db.commit()
    notify_task_update(task_id, ["events"])


def get_events(task_id: Optional[str] = None, limit: int = 200) -> List[dict]:
    with SessionLocal() as db:
        q = db.query(EventModel)
        if task_id:
            q = q.filter(EventModel.task_id == task_id)
        rows = q.order_by(EventModel.timestamp.desc()).limit(limit).all()
        rows.reverse()
        return [
            {
                "event_id": r.event_id,
                "task_id": r.task_id,
                "actor_id": r.actor_id,
                "event_type": r.event_type,
                "payload": json.loads(r.payload or "{}"),
                "timestamp": _dt_to_timestamp(r.timestamp),
            }
            for r in rows
        ]


def get_latest_stage_done_event(task_id: str, stage_name: str) -> Optional[dict]:
    with SessionLocal() as db:
        rows = (
            db.query(EventModel)
            .filter(
                EventModel.task_id == task_id,
                EventModel.event_type == "StageDone",
            )
            .order_by(EventModel.timestamp.desc())
            .limit(200)
            .all()
        )
        for row in rows:
            payload = json.loads(row.payload or "{}")
            if payload.get("stage") != stage_name:
                continue
            return {
                "event_id": row.event_id,
                "task_id": row.task_id,
                "actor_id": row.actor_id,
                "event_type": row.event_type,
                "payload": payload,
                "timestamp": _dt_to_timestamp(row.timestamp),
            }
    return None


def delete_task(task_id: str):
    with SessionLocal() as db:
        db.query(BlackboardEntryModel).filter(BlackboardEntryModel.task_id == task_id).delete()
        db.query(ConversationMessageModel).filter(ConversationMessageModel.task_id == task_id).delete()
        db.query(EventModel).filter(EventModel.task_id == task_id).delete()
        db.query(TaskModel).filter(TaskModel.task_id == task_id).delete()
        db.commit()
    notify_task_update(task_id, ["task", "events", "collaboration"])


def _serialize_json(value: Optional[Dict[str, Any]]) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


def create_conversation_message(
    task_id: str,
    actor_id: str,
    actor_role: str,
    content: str,
    *,
    conversation_id: str,
    stage_name: str | None = None,
    stage_type: str | None = None,
    thread_kind: str = "stage_loop",
    thread_scope: str = "local",
    recipient_id: str | None = None,
    message_type: str = "comment",
    payload: Optional[Dict[str, Any]] = None,
    reply_to: str | None = None,
) -> Dict[str, Any]:
    with SessionLocal() as db:
        last_row = (
            db.query(ConversationMessageModel)
            .filter(ConversationMessageModel.conversation_id == conversation_id)
            .order_by(desc(ConversationMessageModel.turn_index), desc(ConversationMessageModel.created_at))
            .first()
        )
        row = ConversationMessageModel(
            message_id=str(uuid.uuid4()),
            task_id=task_id,
            stage_name=stage_name,
            stage_type=stage_type,
            conversation_id=conversation_id,
            thread_kind=thread_kind,
            thread_scope=thread_scope,
            actor_id=actor_id,
            actor_role=actor_role or "",
            recipient_id=recipient_id,
            message_type=message_type or "comment",
            content=content or "",
            payload=_serialize_json(payload),
            turn_index=int(getattr(last_row, "turn_index", 0) or 0) + 1,
            reply_to=reply_to,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload_dict = {
            "message_id": row.message_id,
            "task_id": row.task_id,
            "stage_name": row.stage_name,
            "stage_type": row.stage_type,
            "conversation_id": row.conversation_id,
            "thread_kind": row.thread_kind,
            "thread_scope": row.thread_scope,
            "actor_id": row.actor_id,
            "actor_role": row.actor_role,
            "recipient_id": row.recipient_id,
            "message_type": row.message_type,
            "content": row.content,
            "payload": json.loads(row.payload or "{}"),
            "turn_index": row.turn_index,
            "reply_to": row.reply_to,
            "created_at": _dt_to_timestamp(row.created_at),
        }
    notify_task_update(task_id, ["collaboration"])
    return payload_dict


def list_conversation_messages(
    task_id: str,
    *,
    stage_name: str | None = None,
    conversation_id: str | None = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        q = db.query(ConversationMessageModel).filter(ConversationMessageModel.task_id == task_id)
        if stage_name:
            q = q.filter(ConversationMessageModel.stage_name == stage_name)
        if conversation_id:
            q = q.filter(ConversationMessageModel.conversation_id == conversation_id)
        rows = q.order_by(desc(ConversationMessageModel.created_at), desc(ConversationMessageModel.turn_index)).limit(limit).all()
        rows = list(reversed(rows))
        return [
            {
                "message_id": row.message_id,
                "task_id": row.task_id,
                "stage_name": row.stage_name,
                "stage_type": row.stage_type,
                "conversation_id": row.conversation_id,
                "thread_kind": row.thread_kind,
                "thread_scope": row.thread_scope,
                "actor_id": row.actor_id,
                "actor_role": row.actor_role,
                "recipient_id": row.recipient_id,
                "message_type": row.message_type,
                "content": row.content,
                "payload": json.loads(row.payload or "{}"),
                "turn_index": row.turn_index,
                "reply_to": row.reply_to,
                "created_at": _dt_to_timestamp(row.created_at),
            }
            for row in rows
        ]


def upsert_blackboard_entry(
    task_id: str,
    entry_key: str,
    *,
    stage_name: str | None = None,
    entry_type: str = "note",
    title: str = "",
    content: str = "",
    payload: Optional[Dict[str, Any]] = None,
    source_message_id: str | None = None,
) -> Dict[str, Any]:
    with SessionLocal() as db:
        row = (
            db.query(BlackboardEntryModel)
            .filter(
                BlackboardEntryModel.task_id == task_id,
                BlackboardEntryModel.entry_key == entry_key,
            )
            .order_by(desc(BlackboardEntryModel.updated_at), desc(BlackboardEntryModel.created_at))
            .first()
        )
        if row is None:
            row = BlackboardEntryModel(
                entry_id=str(uuid.uuid4()),
                task_id=task_id,
                stage_name=stage_name,
                entry_key=entry_key,
                entry_type=entry_type or "note",
                title=title or "",
                content=content or "",
                payload=_serialize_json(payload),
                source_message_id=source_message_id,
            )
            db.add(row)
        else:
            row.stage_name = stage_name
            row.entry_type = entry_type or row.entry_type
            row.title = title or row.title
            row.content = content or ""
            row.payload = _serialize_json(payload)
            row.source_message_id = source_message_id
            row.updated_at = _now()
        db.commit()
        db.refresh(row)
        payload_dict = {
            "entry_id": row.entry_id,
            "task_id": row.task_id,
            "stage_name": row.stage_name,
            "entry_key": row.entry_key,
            "entry_type": row.entry_type,
            "title": row.title,
            "content": row.content,
            "payload": json.loads(row.payload or "{}"),
            "source_message_id": row.source_message_id,
            "created_at": _dt_to_timestamp(row.created_at),
            "updated_at": _dt_to_timestamp(row.updated_at),
        }
    notify_task_update(task_id, ["collaboration"])
    return payload_dict


def list_blackboard_entries(task_id: str, *, stage_name: str | None = None, limit: int = 200) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        q = db.query(BlackboardEntryModel).filter(BlackboardEntryModel.task_id == task_id)
        if stage_name:
            q = q.filter(BlackboardEntryModel.stage_name == stage_name)
        rows = q.order_by(desc(BlackboardEntryModel.updated_at), desc(BlackboardEntryModel.created_at)).limit(limit).all()
        return [
            {
                "entry_id": row.entry_id,
                "task_id": row.task_id,
                "stage_name": row.stage_name,
                "entry_key": row.entry_key,
                "entry_type": row.entry_type,
                "title": row.title,
                "content": row.content,
                "payload": json.loads(row.payload or "{}"),
                "source_message_id": row.source_message_id,
                "created_at": _dt_to_timestamp(row.created_at),
                "updated_at": _dt_to_timestamp(row.updated_at),
            }
            for row in rows
        ]


# ---- AI registry ----
def list_ai_credentials() -> List[dict]:
    with SessionLocal() as db:
        rows = db.query(AiCredentialModel).order_by(desc(AiCredentialModel.updated_at), desc(AiCredentialModel.created_at)).all()
        return [
            {
                "credential_id": r.credential_id,
                "name": r.name,
                "base_url": r.base_url,
                "api_key_env": r.api_key_env,
                "api_key_hint": r.api_key_hint,
                "created_at": _dt_to_iso(r.created_at),
                "updated_at": _dt_to_iso(r.updated_at),
            }
            for r in rows
        ]


def create_ai_credential(name: str, base_url: str, api_key_env: str | None = None, api_key: str | None = None) -> dict:
    with SessionLocal() as db:
        row = AiCredentialModel(
            credential_id=str(uuid.uuid4()),
            name=name,
            base_url=base_url,
            api_key_env=api_key_env or None,
            api_key=api_key or None,
            api_key_hint=_api_key_hint(api_key or os.getenv(api_key_env or "", "")),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "credential_id": row.credential_id,
            "name": row.name,
            "base_url": row.base_url,
            "api_key_env": row.api_key_env,
            "api_key_hint": row.api_key_hint,
        }


def update_ai_credential(credential_id: str, payload: Dict[str, Any]) -> Optional[dict]:
    with SessionLocal() as db:
        row = db.get(AiCredentialModel, credential_id)
        if not row:
            return None
        if "name" in payload and payload.get("name"):
            row.name = payload["name"]
        if "base_url" in payload and payload.get("base_url"):
            row.base_url = payload["base_url"]
        if "api_key_env" in payload:
            row.api_key_env = payload.get("api_key_env") or None
        if "api_key" in payload:
            row.api_key = payload.get("api_key") or None
        row.api_key_hint = _api_key_hint(row.api_key or os.getenv(row.api_key_env or "", ""))
        row.updated_at = _now()
        db.commit()
        db.refresh(row)
        return {
            "credential_id": row.credential_id,
            "name": row.name,
            "base_url": row.base_url,
            "api_key_env": row.api_key_env,
            "api_key_hint": row.api_key_hint,
        }


def delete_ai_credential(credential_id: str) -> bool:
    with SessionLocal() as db:
        model_count = db.query(AiModelModel).filter(AiModelModel.credential_id == credential_id).count()
        if model_count:
            raise ValueError("credential still has models")
        row = db.get(AiCredentialModel, credential_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True


def get_ai_credential_secret(credential_id: str) -> Optional[dict]:
    with SessionLocal() as db:
        row = db.get(AiCredentialModel, credential_id)
        if not row:
            return None
        secret = row.api_key or os.getenv(row.api_key_env or "", "") or ""
        return {
            "credential_id": row.credential_id,
            "name": row.name,
            "base_url": row.base_url,
            "api_key_env": row.api_key_env,
            "api_key": secret,
            "api_key_hint": row.api_key_hint,
        }


def list_ai_models() -> List[dict]:
    with SessionLocal() as db:
        rows = db.query(AiModelModel).order_by(desc(AiModelModel.updated_at), desc(AiModelModel.created_at)).all()
        creds = {r.credential_id: r for r in db.query(AiCredentialModel).all()}
        return [
            {
                "model_id": r.model_id,
                "credential_id": r.credential_id,
                "credential_name": creds.get(r.credential_id).name if creds.get(r.credential_id) else "",
                "name": r.name,
                "provider_type": r.provider_type,
                "model_kind": r.model_kind,
                "extra_config": json.loads(r.extra_config or "{}"),
                "created_at": _dt_to_iso(r.created_at),
                "updated_at": _dt_to_iso(r.updated_at),
            }
            for r in rows
        ]


def create_ai_model(credential_id: str, name: str, provider_type: str = "openai-compatible", model_kind: str = "llm", extra_config: Optional[dict] = None) -> dict:
    with SessionLocal() as db:
        if not db.get(AiCredentialModel, credential_id):
            raise ValueError("credential not found")
        row = AiModelModel(
            model_id=str(uuid.uuid4()),
            credential_id=credential_id,
            name=name,
            provider_type=provider_type or "openai-compatible",
            model_kind=model_kind or "llm",
            extra_config=json.dumps(extra_config or {}, ensure_ascii=False),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "model_id": row.model_id,
            "credential_id": row.credential_id,
            "name": row.name,
            "provider_type": row.provider_type,
            "model_kind": row.model_kind,
            "extra_config": json.loads(row.extra_config or "{}"),
        }


def update_ai_model(model_id: str, payload: Dict[str, Any]) -> Optional[dict]:
    with SessionLocal() as db:
        row = db.get(AiModelModel, model_id)
        if not row:
            return None
        if "credential_id" in payload and payload.get("credential_id"):
            row.credential_id = payload["credential_id"]
        if "name" in payload and payload.get("name"):
            row.name = payload["name"]
        if "provider_type" in payload and payload.get("provider_type"):
            row.provider_type = payload["provider_type"]
        if "model_kind" in payload and payload.get("model_kind"):
            row.model_kind = payload["model_kind"]
        if "extra_config" in payload:
            row.extra_config = json.dumps(payload.get("extra_config") or {}, ensure_ascii=False)
        row.updated_at = _now()
        db.commit()
        db.refresh(row)
        return {
            "model_id": row.model_id,
            "credential_id": row.credential_id,
            "name": row.name,
            "provider_type": row.provider_type,
            "model_kind": row.model_kind,
            "extra_config": json.loads(row.extra_config or "{}"),
        }


def delete_ai_model(model_id: str) -> bool:
    with SessionLocal() as db:
        binding = db.query(AiStageBindingModel).filter(AiStageBindingModel.model_id == model_id).first()
        if binding:
            raise ValueError("model is bound to stage")
        row = db.get(AiModelModel, model_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True


def get_ai_model(model_id: str) -> Optional[dict]:
    with SessionLocal() as db:
        row = db.get(AiModelModel, model_id)
        if not row:
            return None
        return {
            "model_id": row.model_id,
            "credential_id": row.credential_id,
            "name": row.name,
            "provider_type": row.provider_type,
            "model_kind": row.model_kind,
            "extra_config": json.loads(row.extra_config or "{}"),
        }


def get_stage_bindings() -> Dict[str, Optional[str]]:
    with SessionLocal() as db:
        rows = db.query(AiStageBindingModel).all()
        return {r.stage_name: r.model_id for r in rows}


def set_stage_binding(stage_name: str, model_id: Optional[str]) -> dict:
    with SessionLocal() as db:
        row = db.get(AiStageBindingModel, stage_name)
        if row is None:
            row = AiStageBindingModel(stage_name=stage_name, model_id=model_id)
            db.add(row)
        else:
            row.model_id = model_id
            row.updated_at = _now()
        db.commit()
        db.refresh(row)
        return {"stage_name": row.stage_name, "model_id": row.model_id}
