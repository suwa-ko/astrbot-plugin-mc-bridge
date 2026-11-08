"""Immutable notice destinations, scoped to an event/task rather than last chat."""
from __future__ import annotations
import re
from contextvars import ContextVar
from dataclasses import dataclass

REPLY_ROUTE=ContextVar('mc_reply_route',default=None)
UNSET=object()

@dataclass(frozen=True)
class ReplyRoute:
    channel:str
    target:str=''
    connection_id:str=''

def game_route(bridge):
    session=str(getattr(bridge,'session_id','') or '')
    return ReplyRoute('minecraft',connection_id=session) if session else None

def event_route(event,bridge):
    """Transport selection only; original command/tool authorization still applies."""
    try:
        platform=event.get_platform_name()
        if platform=='minecraft':
            session=str(event.get_extra('_mc_connection_id',''))
            if (session and session==getattr(bridge,'session_id','') and
                event.get_extra('_mc_trusted_player','') and
                re.fullmatch(r'[0-9a-fA-F-]{36}',str(event.get_extra('_mc_sender_uuid','')))):
                return ReplyRoute('minecraft',connection_id=session)
        elif platform=='aiocqhttp' and event.is_private_chat():
            target=str(event.unified_msg_origin)
            parts=target.rsplit(':',2)
            sender=str(event.get_sender_id())
            if len(parts)==3 and parts[0] and parts[1]=='FriendMessage' and parts[2]==sender and sender.isdigit():
                return ReplyRoute('qq',target=target)
    except (AttributeError,TypeError,ValueError):
        pass
    return None
