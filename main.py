import asyncio
import os
import json
from typing import List, Dict, Optional
import datetime
import time

import chardet

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Reply, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

@register(
    "astrbot_plugin_file_checker",
    "Foolllll",
    "群文件失效检查",
    "1.1",
    "https://github.com/Foolllll-J/astrbot_plugin_file_checker"
)
class GroupFileCheckerPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)
        self.config = config if config else {}
        self.group_whitelist: List[int] = self.config.get("group_whitelist", [])
        self.group_whitelist = [int(gid) for gid in self.group_whitelist]
        logger.info("插件 [群文件失效检查] 已加载 (终极侦查模式)。")

    # --- 唯一的、用于最终侦查的临时函数 ---
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1)
    async def debug_print_raw_message(self, event: AstrMessageEvent, *args, **kwargs):
        group_id = int(event.get_group_id())
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        # 检查消息中是否包含文件
        has_file = any(isinstance(seg, Comp.File) for seg in event.get_messages())
        
        if has_file:
            logger.info("--- 原始消息 (raw_message) ---")
            try:
                # raw_message 通常是消息段的列表，每个段是一个字典
                raw_msg_data = event.message_obj.raw_message
                logger.info(json.dumps(raw_msg_data, indent=2, ensure_ascii=False))
            except Exception as e:
                logger.error(f"打印 raw_message 失败: {e}", exc_info=True)
                # 如果不是JSON格式，尝试直接打印
                logger.info(event.message_obj.raw_message)
            logger.info("-----------------------------")
    
    # 原有功能函数暂时留空
    async def _handle_file_check_flow(self, event: AstrMessageEvent, file_component: Comp.File):
        pass