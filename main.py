import asyncio
import os
import json
from typing import List, Dict, Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
# 导入 Reply, Plain 以便其他函数正常运行
from astrbot.api.message_components import Reply, Plain 
# 导入用于直接调用API的特定事件类型
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

# =================================================================
# 这是一份用于进行“最终实验”的特殊版本
# 它包含了两个临时的调试函数，用于收集日志
# =================================================================

@register(
    "astrbot_plugin_file_checker",
    "Foolllll",
    "群文件失效检查",
    "1.0.0",
    "https://github.com/Foolllll-J/astrbot_plugin_file_checker"
)
class GroupFileCheckerPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)
        self.config = config if config else {}
        
        self.group_whitelist: List[int] = self.config.get("group_whitelist", [])
        self.group_whitelist = [int(gid) for gid in self.group_whitelist]
        
        self.notify_on_success: bool = self.config.get("notify_on_success", True)
        self.check_delay_seconds: int = self.config.get("check_delay_seconds", 30)
        
        self.download_semaphore = asyncio.Semaphore(5)
        
        logger.info("插件 [群文件失效检查] 已加载 (最终实验模式)。")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent, *args, **kwargs):
        # 暂时禁用插件的主要功能，只让下面的调试函数运行
        pass

    # =================================================================
    # 实验所需的两个临时函数
    # =================================================================
    
    # 临时函数一：用于打印收到的文件消息组件的内部结构
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1)
    async def debug_print_file_component(self, event: AstrMessageEvent, *args, **kwargs):
        group_id = int(event.get_group_id())
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                logger.info(f"--- 文件消息组件 __dict__ ---\n{segment.__dict__}\n--------------------")
                # 只需要处理一次，避免重复打印
                break

    # 临时函数二：用于调用API获取群文件列表
    @filter.command("testfiles")
    async def test_files_command(self, event: AstrMessageEvent):
        """一个用于测试获取群文件列表的临时指令"""
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            group_id = int(event.get_group_id())
            
            payloads = {"group_id": group_id}
            logger.info(f"正在调用API: 'get_group_root_files'，参数: {payloads}")
            
            ret = await client.api.call_action('get_group_root_files', **payloads)
            
            logger.info(f"--- get_group_root_files API 返回结果 ---\n{json.dumps(ret, indent=2, ensure_ascii=False)}\n--------------------")
            yield event.plain_result("API结果已打印到后台日志。")
        except Exception as e:
            logger.error(f"调用API失败: {e}", exc_info=True)
            yield event.plain_result(f"调用API失败: {e}")

    # =================================================================
    # 原有功能函数暂时保留，但不会被调用
    # =================================================================
    async def _handle_file_check(self, event: AstrMessageEvent, file_name: str, file_component: Comp.File):
        pass

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")