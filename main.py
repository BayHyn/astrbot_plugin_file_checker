import asyncio
import os
import json
from typing import List, Dict, Optional
import datetime # 导入时间处理库

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Reply, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

# =================================================================
# 这是一个用于进行“最终实验”的特殊版本 (V2 - 增强日志)
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
        logger.info("插件 [群文件失效检查] 已加载 (最终实验模式 V2 - 增强日志)。")

    # --- 新增: 时间戳转换工具函数 ---
    def _timestamp_to_str(self, timestamp: int) -> str:
        """将Unix时间戳转换为本地时区的可读字符串"""
        try:
            # 使用 fromtimestamp 创建一个本地时间的 datetime 对象
            dt_object = datetime.datetime.fromtimestamp(timestamp)
            # 格式化为 "年-月-日 时:分:秒"
            return dt_object.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return f"无法转换的时间戳: {timestamp}"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1)
    async def debug_print_file_component(self, event: AstrMessageEvent, *args, **kwargs):
        group_id = int(event.get_group_id())
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                # --- 新增: 获取并记录收到消息的当前时间 ---
                received_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"--- 文件消息组件 __dict__ (接收时间: {received_time_str}) ---")
                logger.info(f"{segment.__dict__}")
                logger.info("--------------------")
                break

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
            
            # --- 修改点: 在打印结果前，先转换时间戳 ---
            if ret and 'files' in ret and isinstance(ret['files'], list):
                logger.info("--- get_group_root_files API 返回结果 (带时间转换) ---")
                for file_info in ret['files']:
                    if 'modify_time' in file_info:
                        # 为每个文件信息新增一个可读的时间字段
                        file_info['modify_time_str'] = self._timestamp_to_str(file_info['modify_time'])
            
            logger.info(f"{json.dumps(ret, indent=2, ensure_ascii=False)}")
            logger.info("--------------------")

            yield event.plain_result("API结果已打印到后台日志。")
        except Exception as e:
            logger.error(f"调用API失败: {e}", exc_info=True)
            yield event.plain_result(f"调用API失败: {e}")

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")