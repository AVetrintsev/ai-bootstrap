"""Initialize agent settings without running a turn (Agent Server 1.62.0)."""

import asyncio
import logging
from uuid import UUID

from fastapi import Depends, Request

from openhands.agent_server.conversation_router import conversation_router, get_conversation
from openhands.agent_server.conversation_service import ConversationService
from openhands.agent_server.dependencies import get_conversation_service
from openhands.agent_server.models import ConversationInfo
from openhands.sdk.agent import ACPAgent


logger = logging.getLogger(__name__)
QWEN_COMMAND = ("/usr/local/bin/qwen",)
PROJECT_AGENT_COMMANDS = {("/usr/local/bin/codex",), QWEN_COMMAND}


@conversation_router.get("/{conversation_id}/agent_options")
async def agent_options(
    conversation_id: UUID,
    request: Request,
    include_skills: bool = False,
    service: ConversationService = Depends(get_conversation_service),
) -> ConversationInfo:
    info = await get_conversation(conversation_id, request, include_skills, service)
    agent = info.agent
    if (
        isinstance(agent, ACPAgent)
        and tuple(agent.acp_command) in PROJECT_AGENT_COMMANDS
        and tuple(agent.acp_args) == ("--acp",)
        # A saved Qwen catalog can describe a previous gateway configuration.
        and (tuple(agent.acp_command) == QWEN_COMMAND or not info.available_models or not info.available_efforts)
    ):
        try:
            events = await service.get_event_service(conversation_id)
            if events is not None and events._conversation is not None:
                # Same locked startup as the first turn. It performs the ACP
                # handshake and restores settings without prompting the model.
                await asyncio.to_thread(events._conversation._ensure_agent_ready)
                return await get_conversation(conversation_id, request, include_skills, service)
        except Exception as error:
            logger.warning("Could not load ACP agent settings (%s)", type(error).__name__)
    return info
