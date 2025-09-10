import asyncio
import os
from typing import List, Dict, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

@register(
    "astrbot_plugin_file_checker",
    "Foolllll",
    "群文件失效检查",
    "1.0",
    "https://github.com/Foolllll-J/astrbot_plugin_file_checker"
)
class GroupFileCheckerPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)
        self.config = config if config else {}
        
        self.group_whitelist: List[int] = self.config.get("group_whitelist", [])
        self.group_whitelist = [int(gid) for gid in self.group_whitelist]
        
        self.download_semaphore = asyncio.Semaphore(5)
        
        logger.info("插件 [群文件失效检查] 已加载。")
        
        if self.group_whitelist:
            logger.info(f"已启用群聊白名单，只在以下群组生效: {self.group_whitelist}")
        else:
            logger.info("未配置群聊白名单，插件将在所有群组生效。")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = int(event.get_group_id())
        
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                file_name = segment.data.get('name', '未知文件名')
                logger.info(f"检测到群 {group_id} 中的文件消息: '{file_name}'，已加入处理队列。")
                asyncio.create_task(self._handle_file_check(group_id, file_name, segment))
                break

    async def _handle_file_check(self, group_id: int, file_name: str, file_component: Comp.File):
        await asyncio.sleep(30)
        
        async with self.download_semaphore:
            logger.info(f"[{group_id}] 开始检查文件: '{file_name}' (获得处理许可)")
            try:
                local_file_path = await file_component.get_file()
                logger.info(f"✅ [{group_id}] 文件 '{file_name}' 检查有效，已下载到: {local_file_path}")
                try:
                    os.remove(local_file_path)
                    logger.info(f"[{group_id}] 临时文件 '{local_file_path}' 已被删除。")
                except OSError as e:
                    logger.warning(f"[{group_id}] 删除临时文件失败: {e}")

            except Exception as e:
                logger.error(f"❌ [{group_id}] 文件 '{file_name}' 经检查已失效! 框架返回错误: {e}")
                try:
                    failure_message = f"刚刚发送的文件「{file_name}」经检查已失效或被服务器屏蔽。"
                    await self.context.send_group_message(group_id=group_id, message=failure_message)
                    logger.info(f"已向群 {group_id} 发送文件失效通知。")
                except Exception as send_e:
                    logger.error(f"向群 {group_id} 发送失效通知时再次发生错误: {send_e}")

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")